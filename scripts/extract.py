"""Download selected official RBA statistical tables for inflation research."""

from __future__ import annotations

import hashlib
import io
import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
import openpyxl

from scripts.config import REQUEST_TIMEOUT, USER_AGENT
from scripts.time_series import Observation

logger = logging.getLogger(__name__)
COUNTRY_CURRENCY = "AUD"
SOURCE_ROOT = "https://www.rba.gov.au/statistics/tables/"
TABLES = {
    "F01": "https://www.rba.gov.au/statistics/tables/xls/f01hist.xlsx",
    "F15": "https://www.rba.gov.au/statistics/tables/xls/f15hist.xlsx",
}
# Native RBA IDs: curated predictors with an economic link to inflation.
SELECTED = {
    "F01": frozenset({"FIRMMCRT", "FIRMMCRI"}),
    "F15": frozenset({"FRERTWI", "FRERIWI"}),
}
MAX_STALE_MONTHS = {"monthly": 3, "quarterly": 6}
MIN_HISTORY_YEARS = {"monthly": 3, "quarterly": 5}


@dataclass(frozen=True)
class SourceData:
    """Canonical observations and source-derived descriptors."""

    observations: list[Observation]
    catalog: dict[str, dict[str, Any]]


def build_series_id(table: str, native_id: str) -> str:
    """Preserve the official identifier within the RBA table namespace."""
    if table not in TABLES or not native_id.isalnum() or native_id != native_id.upper():
        raise ValueError(f"Invalid RBA identifier: {table!r}, {native_id!r}")
    return f"RBA_{table}_{native_id}"


def parse_series_id(series_id: str) -> tuple[str, str]:
    """Recover the RBA table and native series identifier."""
    parts = series_id.split("_")
    if len(parts) != 3 or parts[0] != "RBA":
        raise ValueError(f"Invalid RBA series_id: {series_id}")
    table, native_id = parts[1:]
    if build_series_id(table, native_id) != series_id:
        raise ValueError(f"Invalid RBA series_id: {series_id}")
    return table, native_id


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            day, month, year = value.split("-")
            months = {name: index for index, name in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
            return date(int(year), months[month], int(day))
    raise ValueError(f"Unexpected RBA date: {value!r}")


def parse_workbook(blob: bytes, table: str) -> SourceData:
    """Parse native IDs and source labels without shifting dates or units."""
    sheet = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)["Data"]
    rows = iter(sheet.values)
    header = [next(rows) for _ in range(11)]
    labels = ["Title", "Description", "Frequency", "Type", "Units", "Source", "Publication date", "Series ID"]
    if [header[i][0] for i in (1, 2, 3, 4, 5, 8, 9, 10)] != labels:
        raise ValueError(f"RBA {table} header changed")
    columns: dict[int, dict[str, Any]] = {}
    for index, native in enumerate(header[10]):
        if index == 0 or native not in SELECTED[table]:
            continue
        frequency = str(header[3][index]).lower()
        unit = "index" if str(header[5][index]).startswith("Index") else "percent"
        if frequency not in MAX_STALE_MONTHS or (unit == "percent" and header[5][index] != "Per cent"):
            raise ValueError(f"Unexpected RBA {table} frequency or unit for {native}")
        if header[8][index] != "RBA":
            raise ValueError(f"Unexpected upstream owner for {native}: {header[8][index]}")
        sid = build_series_id(table, str(native))
        columns[index] = {
            "series_id": sid,
            "name": str(header[1][index]),
            "description": str(header[2][index]),
            "country": COUNTRY_CURRENCY,
            "frequency": frequency,
            "unit": unit,
            "eco_group": "interest_rates" if table == "F01" else "exchange_rates",
            "source_url": TABLES[table],
            "last_publish_date": _date(header[9][index]),
        }
    if {parse_series_id(v["series_id"])[1] for v in columns.values()} != SELECTED[table]:
        raise ValueError(f"RBA {table} missing selected official IDs")
    snapshot = hashlib.sha256(blob).hexdigest()
    observations: list[Observation] = []
    for row in rows:
        if not isinstance(row[0], datetime | date):
            continue
        reference_date = _date(row[0])
        for index, fields in columns.items():
            value = row[index]
            if value is None or value == "":
                continue
            if not isinstance(value, int | float) or not math.isfinite(value):
                raise ValueError(f"Unexpected value {value!r} for {fields['series_id']} at {reference_date}")
            observations.append(Observation(fields["series_id"], reference_date, float(value), snapshot))
    if not observations:
        raise ValueError(f"RBA {table} yielded no numeric observations")
    return SourceData(observations, {v["series_id"]: v for v in columns.values()})


def filter_usable_series(data: SourceData, today: date) -> SourceData:
    """Reject stale or short non-null histories by source frequency."""
    dates: dict[str, list[date]] = {}
    for item in data.observations:
        dates.setdefault(item.series_id, []).append(item.reference_date)
    kept: set[str] = set()
    for sid, points in dates.items():
        frequency = data.catalog[sid]["frequency"]
        last = max(points)
        first = min(points)
        stale_months = (today.year - last.year) * 12 + today.month - last.month
        history_months = (last.year - first.year) * 12 + last.month - first.month
        if stale_months <= MAX_STALE_MONTHS[frequency] and history_months >= MIN_HISTORY_YEARS[frequency] * 12:
            kept.add(sid)
        else:
            logger.warning("Excluded %s: stale_months=%d history_months=%d", sid, stale_months, history_months)
    if not kept:
        raise ValueError("No active RBA series with sufficient history")
    return SourceData([o for o in data.observations if o.series_id in kept], {sid: v for sid, v in data.catalog.items() if sid in kept})


def collect() -> SourceData:
    """Fetch each official workbook once, failing loudly on source errors."""
    observations: list[Observation] = []
    catalog: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        for table, url in TABLES.items():
            response = client.get(url)
            response.raise_for_status()
            parsed = parse_workbook(response.content, table)
            observations.extend(parsed.observations)
            catalog.update(parsed.catalog)
            logger.info("%s: %d observations from %s", table, len(parsed.observations), url)
    return filter_usable_series(SourceData(observations, catalog), datetime.now(UTC).date())
