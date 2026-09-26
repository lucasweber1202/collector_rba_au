# collector_rba_au

Selected official RBA monthly cash rates and quarterly real exchange-rate indices for Australian inflation research.

Source: https://www.rba.gov.au/statistics/tables/

## Quickstart

Create a Python 3.11 virtual environment and run `pip install -e .`. Copy `.env.example` to `.env` and configure `COLLECTOR_DB_URL` for PostgreSQL or `PROD=true` with the Databricks settings. Run `python main.py` for the default incremental window, or `python main.py --start-date 1970-01-01` for a full backfill. Each collector uses its own `collector_rba_au` schema.
