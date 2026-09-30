"""Download selected official RBA statistical tables for inflation research."""

from __future__ import annotations

import hashlib
import io
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
import openpyxl

from scripts.config import BACKOFF_FACTOR, DOWNLOAD_DELAY, MAX_RETRIES, REQUEST_TIMEOUT, USER_AGENT
from scripts.releases import ReleaseEvidence
from scripts.time_series import Observation

# Canonical metadata vocabulary produced by this source.
FREQUENCIES: frozenset[str] = frozenset({"monthly", "quarterly"})
UNITS: frozenset[str] = frozenset({"percent", "index"})
ECO_GROUPS: frozenset[str] = frozenset({"interest_rates", "exchange_rates"})


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
MIN_PAYLOAD_BYTES = 20_000


class SourceLayoutError(ValueError):
    """An RBA workbook no longer has the audited layout."""


class SourceAccessError(RuntimeError):
    """The source answered with something other than the requested workbook."""


@dataclass(frozen=True)
class SourceData:
    """Canonical observations and source-derived descriptors."""

    observations: list[Observation]
    catalog: dict[str, dict[str, Any]]
    releases: tuple[ReleaseEvidence, ...] = ()


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
            months = {
                name: index
                for index, name in enumerate(
                    (
                        "Jan",
                        "Feb",
                        "Mar",
                        "Apr",
                        "May",
                        "Jun",
                        "Jul",
                        "Aug",
                        "Sep",
                        "Oct",
                        "Nov",
                        "Dec",
                    ),
                    1,
                )
            }
            return date(int(year), months[month], int(day))
    raise ValueError(f"Unexpected RBA date: {value!r}")


def parse_workbook(blob: bytes, table: str) -> SourceData:
    """Parse native IDs and source labels without shifting dates or units."""
    workbook = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    if "Data" not in workbook:
        raise SourceLayoutError(f"RBA {table} Data sheet missing")
    sheet = workbook["Data"]
    rows = iter(sheet.values)
    header = [next(rows) for _ in range(11)]
    labels = [
        "Title",
        "Description",
        "Frequency",
        "Type",
        "Units",
        "Source",
        "Publication date",
        "Series ID",
    ]
    if [header[i][0] for i in (1, 2, 3, 4, 5, 8, 9, 10)] != labels:
        raise SourceLayoutError(f"RBA {table} header changed")
    columns: dict[int, dict[str, Any]] = {}
    for index, native in enumerate(header[10]):
        if index == 0 or native not in SELECTED[table]:
            continue
        frequency = str(header[3][index]).lower()
        unit = "index" if str(header[5][index]).startswith("Index") else "percent"
        if frequency not in MAX_STALE_MONTHS or (
            unit == "percent" and header[5][index] != "Per cent"
        ):
            raise SourceLayoutError(f"Unexpected RBA {table} frequency or unit for {native}")
        if header[8][index] != "RBA":
            raise SourceLayoutError(f"Unexpected upstream owner for {native}: {header[8][index]}")
        sid = build_series_id(table, str(native))
        columns[index] = {
            "series_id": sid,
            "name": str(header[1][index]),
            "description": str(header[2][index]),
            "country": COUNTRY_CURRENCY,
            "frequency": frequency,
            "unit": unit,
            "eco_group": "interest_rates" if table == "F01" else "exchange_rates",
            "source_url": "https://www.rba.gov.au/statistics/tables/",
            "last_publish_date": _date(header[9][index]),
        }
    if {parse_series_id(v["series_id"])[1] for v in columns.values()} != SELECTED[table]:
        raise SourceLayoutError(f"RBA {table} missing selected official IDs")
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
                raise ValueError(
                    f"Unexpected value {value!r} for {fields['series_id']} at {reference_date}"
                )
            observations.append(
                Observation(fields["series_id"], reference_date, float(value), snapshot)
            )
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
        if (
            stale_months <= MAX_STALE_MONTHS[frequency]
            and history_months >= MIN_HISTORY_YEARS[frequency] * 12
        ):
            kept.add(sid)
        else:
            logger.warning(
                "Excluded %s: stale_months=%d history_months=%d", sid, stale_months, history_months
            )
    if not kept:
        raise ValueError("No active RBA series with sufficient history")
    return SourceData(
        [o for o in data.observations if o.series_id in kept],
        {sid: v for sid, v in data.catalog.items() if sid in kept},
    )


def check_payload(response: httpx.Response) -> bytes:
    """Refuse an HTML challenge or error page before it can be parsed as XLSX."""
    response.raise_for_status()
    blob = response.content
    content_type = response.headers.get("content-type", "").lower()
    head = blob[:512].lstrip().lower()
    if "text/html" in content_type or head.startswith((b"<!doctype", b"<html")):
        raise SourceAccessError(f"RBA returned HTML instead of a workbook: {response.url}")
    if not blob.startswith(b"PK\x03\x04"):
        raise SourceAccessError(f"RBA workbook is not an XLSX file: {response.url}")
    if len(blob) < MIN_PAYLOAD_BYTES:
        raise SourceAccessError(f"RBA workbook is implausibly small ({len(blob)} bytes)")
    return blob


def collect() -> SourceData:
    """Fetch each official workbook once, failing loudly on source errors."""
    observations: list[Observation] = []
    catalog: dict[str, dict[str, Any]] = {}
    parsed_tables: dict[str, SourceData] = {}
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        for table, url in TABLES.items():
            parsed = parse_workbook(check_payload(_http_get(client, url)), table)
            parsed_tables[table] = parsed
            observations.extend(parsed.observations)
            catalog.update(parsed.catalog)
            logger.info("%s: %d observations from %s", table, len(parsed.observations), url)
    usable = filter_usable_series(SourceData(observations, catalog), datetime.now(UTC).date())
    evidence = []
    for table, parsed in parsed_tables.items():
        ids = frozenset(parsed.catalog) & frozenset(usable.catalog)
        if not ids:
            continue
        # The workbook's own "Publication date" header row, per selected column.
        published = max(usable.catalog[sid]["last_publish_date"] for sid in ids)
        latest = max(o.reference_date for o in usable.observations if o.series_id in ids)
        evidence.append(ReleaseEvidence(table, TABLES[table], published, latest, ids))
    return SourceData(usable.observations, usable.catalog, tuple(evidence))


def _http_get(client: httpx.Client, url: str) -> httpx.Response:
    """Retry transport failures, HTTP 429 and 5xx; preserve source-specific checks."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(DOWNLOAD_DELAY)
            response = client.get(url)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == MAX_RETRIES:
                raise
            wait = BACKOFF_FACTOR ** attempt
            logging.getLogger(__name__).warning(
                "GET failed, retry %d/%d in %.1fs", attempt, MAX_RETRIES, wait
            )
            time.sleep(wait)
    raise RuntimeError("COLLECTOR_MAX_RETRIES must be positive")


UPSTREAM_METADATA: dict[str, dict[str, Any]] = {}


def collect_raw_data(start_date: date | None = None) -> dict[date, dict[str, float | None]]:
    """Expose the canonical mapping and refresh upstream descriptors on every call."""
    UPSTREAM_METADATA.clear()
    data = collect()
    UPSTREAM_METADATA.update(data.catalog)
    parsed: dict[date, dict[str, float | None]] = {}
    for item in data.observations:
        if start_date is None or item.reference_date >= start_date:
            parsed.setdefault(item.reference_date, {})[item.series_id] = item.value
    logging.getLogger(__name__).info("Parsed %d dates", len(parsed))
    return parsed
