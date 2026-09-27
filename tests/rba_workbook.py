"""Build small workbooks in the audited RBA statistical-table layout."""

from __future__ import annotations

import io
from datetime import date

import openpyxl

SERIES = {
    "F01": [("FIRMMCRT", "Cash Rate Target", "Monthly", "Per cent"), ("FIRMMCRI", "Interbank Overnight Cash Rate", "Monthly", "Per cent")],
    "F15": [("FRERTWI", "Real Trade-Weighted Index", "Quarterly", "Index, March 1995 = 100"), ("FRERIWI", "Real Import-Weighted Index", "Quarterly", "Index, March 1995 = 100")],
}


def workbook(table: str, rows: list[tuple[date, list[float | None]]], published: date = date(2026, 9, 1), owner: str = "RBA") -> bytes:
    series = SERIES[table]
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Data"
    sheet.append([f"{table} TABLE"])
    sheet.append(["Title", *[s[1] for s in series]])
    sheet.append(["Description", *[s[1] + " description" for s in series]])
    sheet.append(["Frequency", *[s[2] for s in series]])
    sheet.append(["Type", *["Original"] * len(series)])
    sheet.append(["Units", *[s[3] for s in series]])
    sheet.append([None])
    sheet.append([None])
    sheet.append(["Source", *[owner] * len(series)])
    sheet.append(["Publication date", *[published] * len(series)])
    sheet.append(["Series ID", *[s[0] for s in series]])
    for when, values in rows:
        sheet.append([when, *values])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def monthly(months: int = 48) -> bytes:
    rows: list[tuple[date, list[float | None]]] = []
    for m in range(months):
        year, month0 = divmod(2022 * 12 + 8 + m, 12)
        rows.append((date(year, month0 + 1, 28), [3.5 + 0.01 * m, None if m == 0 else 3.51 + 0.01 * m]))
    return workbook("F01", rows)
