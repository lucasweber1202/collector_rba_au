# RBA inflation predictors

Authority: `guimasuko/collector_template` main `4bc65765cedd9c14aec196cff382df6dfb318c77`; `AUD` is in its `metadata.country` vocabulary and is what this collector emits.

## Source and selection

The collector reads official RBA statistical workbooks [F1.1](https://www.rba.gov.au/statistics/tables/xls/f01hist.xlsx) and [F15](https://www.rba.gov.au/statistics/tables/xls/f15hist.xlsx). It retains the published cash-rate target and interbank overnight cash rate (monetary policy and funding conditions), plus the real trade-weighted and import-weighted exchange rate indices (imported-price pressure). These are four selected predictors, not an inventory of every RBA table. Each `series_id` is `RBA_<table>_<official RBA series code>`.

F1.1 is monthly and F15 quarterly. The parser uses each workbook's `Data` sheet, official `Series ID`, `Title`, `Description`, `Frequency`, `Units`, `Source`, and `Publication date` rows. It accepts the selected columns only when their upstream source is `RBA`; source dates are stored separately from reference dates. Missing cells do not become observations, and nonfinite or malformed numeric cells fail the run. The source pages state that statistical tables can be revised or withdrawn; neither workbook is an immutable historical release archive. A first backfill therefore gets the collection day's vintage, never a fabricated historical publication vintage.

The selection filter checks only non-null numeric observations. Monthly series require at least three years and a latest observation no older than three calendar months; quarterly series require five years and a latest observation within six months. These thresholds reflect the source cadences. The full workbook is read on each run so discontinued series cannot appear current merely because a recent blank row exists; the start-date window is applied after filtering.

## Verification snapshot

On 2026-09-26 UTC, the two live workbooks exposed four retained series and 1,487 numeric observations dated 1970-06-30 through 2026-08-31. No selected series was dropped. A direct comparison of the first, median, and latest numeric cells of each selected official workbook column against parsed observations is the opt-in `RBA_LIVE_SMOKE=1` test. These are observations in today's published files, not proof that their historical forms were available at those dates. Native source releases and publication dates should be rechecked at run time.

| RBA code | First value | Intermediate value | Latest value |
| --- | --- | --- | --- |
| FIRMMCRT | 1990-08-31: 14.0 | 2008-08-31: 7.25000202521391 | 2026-08-31: 4.35 |
| FIRMMCRI | 1976-05-31: 7.706 | 2001-07-31: 5.0 | 2026-08-31: 4.35 |
| FRERTWI | 1970-06-30: 146.45132065177313 | 1998-06-30: 105.553068479916 | 2026-06-30: 157.31890990074345 |
| FRERIWI | 1970-06-30: 141.12888429538572 | 1998-06-30: 100.79449284775535 | 2026-06-30: 146.36258086692246 |

PIT mode is `current-backfill` for first ingestion, followed by observed collector vintages on subsequent runs. No past as-of view exists before the first collection. The source does not supply per-observation release timestamps or a revision event feed in these workbooks; `last_publish_date` comes from their official publication-date header and may cover the workbook as a whole.

## Payload, layout and history

`check_payload` refuses HTML/challenge pages whatever their `Content-Type`, a
body without the XLSX `PK\x03\x04` magic and an implausibly small file. A
missing `Data` sheet, a changed header, an unexpected frequency/unit, an
upstream owner other than `RBA` or a missing selected ID raises
`SourceLayoutError`; the run stops before writing and logs
`release_status=layout_changed`. The default start date is now 1970-01-01:
the previous 1999 default silently dropped 1970–1998 on a first run (603 of
1,487 observations), contradicting the coverage stated above.

## Release monitoring

Each workbook is classified on every run from its own `Publication date`
header row (latest across the selected columns) and latest covered period,
against `metadata` before the run and the rows changed: `first_release`,
`same_release`, `new_release`, `revised_source` or `layout_changed`. An
unchanged rerun on a later day is `same_release`; a publication date that goes
backwards fails the run inside the write transaction. Observed on 2026-09-27:
F1.1 published 2026-09-01 (data to 2026-08-31), F15 published 2026-08-04 (data
to 2026-06-30).

## PostgreSQL and SQL grammar evidence (2026-09-27)

- PostgreSQL 16.13, live `main.py`: run 1 wrote 1,487 observations and 4
  metadata rows (`first_release`); run 2 wrote nothing (`same_release`).
- `tests/test_postgres_integration.py`: canonical tables, idempotent rerun,
  later-day vintage, same-day overwrite, metadata MERGE with NULL in every
  nullable column, time-series MERGE, run log, release classification.
- All emitted SQL parses with the Spark SQL grammar (pyspark 4.1.1).
  **Databricks corporate runtime: not verified.**

## Masuko authority verification

Pinned authority: `guimasuko/collector_template@4bc65765cedd9c14aec196cff382df6dfb318c77`. Physical `.github/` and `.vscode/` paths are checked against Git blobs. `.gitignore` and `scripts/databricks_engine.py` have no physical path in the template tree; they are canonical fenced blocks in `GUIDELINES.md` sections 8.1 and 8.9. The guideline Git blob is `089fbbca6a2241d3f02777b82631fbf81d49f6e0`; the two derived file blobs are `f0d1368264d24d7959d3137d618930a06f33795e` and `73821f7a530ab5cca2f5313180d71c17173e6e59`. `tests/test_architecture.py` checks all local blobs on every run. For independent source derivation, check out the exact authority commit and run `MASUKO_TEMPLATE_DIR=/path/to/collector_template python -m pytest -q tests/test_architecture.py`. This checks the guideline blob, extracts both fenced blocks and checks their hashes.
