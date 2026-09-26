"""Check official RBA spreadsheet headers, dates, values, and series identities."""

from __future__ import annotations

import io
import os
from datetime import date, datetime

import httpx
import openpyxl
import pytest

from scripts.extract import TABLES, build_series_id, parse_series_id, parse_workbook


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
    sheet = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True, data_only=True)["Data"]
    rows = list(sheet.values)
    target_id = "FIRMMCRT" if table == "F01" else "FRERTWI"
    column = rows[10].index(target_id)
    official = [(r[0].date() if isinstance(r[0], datetime) else r[0], r[column]) for r in rows[11:] if isinstance(r[0], datetime | date) and isinstance(r[column], int | float)]
    collected = {(o.reference_date, o.series_id): o.value for o in parsed.observations}
    for ref, expected in (official[0], official[len(official) // 2], official[-1]):
        assert collected[(ref, build_series_id(table, target_id))] == expected
