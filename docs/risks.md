# Risk register (D0)

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-01 | 5-day deadline cannot fit every acceptance scenario | High | High | Priority order (assumptions A-03); each stage ships tests + docs so partial delivery is coherent |
| R-02 | Reviewers' spreadsheet has unexpected layout | High | Medium | Mapping-driven importer, header aliases, per-row error log, dry-run before commit |
| R-03 | Migration damages existing data | Low | Critical | Staging area, single-transaction commit with batch ID, rollback by batch, pre-migration backup (NFR01) |
| R-04 | Offline update/rollback breaks the DB schema | Medium | High | Versioned Alembic migrations with tested downgrade; backup taken automatically before update |
| R-05 | Mock-only integration hides real-vendor issues | Medium | Medium | Narrow connector/device interfaces; state limitation explicitly; no certification claims |
| R-06 | SQLite write contention under gate/inventory bursts | Low | Medium | WAL + busy timeout; event queue serialises writes; Postgres path documented |

## Dependencies
Python 3.12+, FastAPI, Uvicorn, SQLAlchemy, Alembic, Jinja2, python-multipart, openpyxl (pinned in `pyproject.toml`).

## Excluded work
Hardware, production systems/data, certifications (SOP section 3).
