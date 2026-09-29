"""Check official RBA spreadsheet headers, dates, values, and series identities."""

from __future__ import annotations

import io
import os
from datetime import date, datetime

import httpx
import openpyxl
import pytest

from scripts.extract import (
    TABLES,
    SourceAccessError,
    SourceLayoutError,
    build_series_id,
    check_payload,
    parse_series_id,
    parse_workbook,
)
from tests.rba_workbook import monthly, workbook


def test_series_id_roundtrip() -> None:
    for table, native in (("F01", "FIRMMCRT"), ("F15", "FRERTWI")):
        sid = build_series_id(table, native)
        assert parse_series_id(sid) == (table, native)
    with pytest.raises(ValueError):
        parse_series_id("RBA_F01_WRONG_extra")


@pytest.mark.skipif(os.getenv("RBA_LIVE_SMOKE") != "1", reason="opt-in official network smoke")
@pytest.mark.parametrize("table", ["F01", "F15"])
def test_live_official_cells(table: str) -> None:
    response = httpx.get(TABLES[table], timeout=30)
    response.raise_for_status()
    parsed = parse_workbook(response.content, table)
    sheet = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True, data_only=True)[
        "Data"
    ]
    rows = list(sheet.values)
    target_id = "FIRMMCRT" if table == "F01" else "FRERTWI"
    column = rows[10].index(target_id)
    official = [
        (r[0].date() if isinstance(r[0], datetime) else r[0], r[column])
        for r in rows[11:]
        if isinstance(r[0], datetime | date) and isinstance(r[column], int | float)
    ]
    collected = {(o.reference_date, o.series_id): o.value for o in parsed.observations}
    for ref, expected in (official[0], official[len(official) // 2], official[-1]):
        assert collected[(ref, build_series_id(table, target_id))] == expected


def test_parse_selected_columns_and_blank_cells() -> None:
    data = parse_workbook(monthly(), "F01")
    cash = data.catalog[build_series_id("F01", "FIRMMCRT")]
    assert (cash["frequency"], cash["unit"], cash["eco_group"], cash["country"]) == (
        "monthly",
        "percent",
        "interest_rates",
        "AUD",
    )
    assert cash["last_publish_date"] == date(2026, 9, 1)
    interbank = [o for o in data.observations if o.series_id == build_series_id("F01", "FIRMMCRI")]
    assert len(interbank) == 47  # the blank first cell is absent, not zero


def test_layout_drift_is_a_layout_error() -> None:
    with pytest.raises(SourceLayoutError, match="owner"):
        parse_workbook(workbook("F01", [(date(2026, 8, 31), [3.6, 3.6])], owner="ABS"), "F01")
    with pytest.raises(SourceLayoutError, match="missing selected"):
        parse_workbook(workbook("F15", [(date(2026, 6, 30), [150.0, 140.0])]), "F01")


def _response(body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://www.rba.gov.au/f.xlsx"),
    )


def test_payload_check_rejects_html_and_non_xlsx() -> None:
    with pytest.raises(SourceAccessError, match="HTML"):
        check_payload(_response(b"<html><body>blocked</body></html>" * 1000, "text/html"))
    with pytest.raises(SourceAccessError, match="not an XLSX"):
        check_payload(_response(b"x" * 30000, "application/octet-stream"))
    with pytest.raises(SourceAccessError, match="small"):
        check_payload(_response(b"PK\x03\x04", "application/vnd.ms-excel"))
