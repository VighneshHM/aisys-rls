# AISYS RFID Library

RFID-enabled library add-on that integrates with an existing ILMS through connector interfaces, with mock adapters for
every device and service that is unavailable. Built for the AISYS Candidate Software Development SOP.

**Status:** foundation (auth, RBAC, audit, health, migrations) and **AC01 spreadsheet migration** complete. See `docs/traceability.md` for progress per
requirement and `docs/assumptions.md` for decisions.

## Quick start (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\aisys seed-demo     # migrates the DB and loads SYNTHETIC users/cards
.\.venv\Scripts\aisys run           # http://127.0.0.1:8000  (API docs at /docs)
```

Demo staff card UIDs (synthetic): `STAFFADMIN01`, `STAFFLIB0001`, `STAFFCLERK01`, `STAFFAUDIT01`.
Patron card `PATRON000001` is rejected at login by design. Demo admin password: see `src/aisys/cli.py`.

## Try the spreadsheet import (AC01)
Sign in as `STAFFLIB0001` (librarian) and open **Data migration**, then upload `samples/library_200.xlsx` (or
`samples/library_20000.xlsx`, the 20k-row file). Review the problems, commit, check the reconciliation, and roll back if you like.
Details, rules and evidence: `docs/migration.md`. Regenerate samples with
`.\.venv\Scripts\python -m aisys.devtools.sample_data --rows 20000 --out samples`.

## Develop

```powershell
.\.venv\Scripts\python -m pytest          # tests (real migrations, temp DB)
.\.venv\Scripts\python -m ruff check .    # lint
.\.venv\Scripts\python scripts\trace.py   # regenerate docs/traceability.md
```

## Configuration
Copy `config/config.sample.toml` to `config/config.toml`. Secrets come from the environment only
(`AISYS_SECRET_KEY`, required in production). Override paths with `AISYS_CONFIG`, `AISYS_DATABASE_URL`, `AISYS_ENV`.

## Layout
```
src/aisys/
  app.py, routes.py, deps.py      web app factory, HTTP API, auth/RBAC dependencies
  auth/                           password hashing, login service, permission catalogue
  integrations/credentials.py     smart-card provider interface + mock
  models.py, db.py, migrations/   ORM, engine, versioned Alembic migrations
  audit.py, logging_setup.py      append-only audit trail, masked structured logs
docs/                             requirements, traceability, assumptions, risks
tests/                            pytest suite, tagged with requirement IDs
```
