# AISYS RFID Library

RFID-enabled library add-on that integrates with an existing ILMS through connector interfaces, with mock adapters for
every device and service that is unavailable. Built for the AISYS Candidate Software Development SOP.

**Status:** foundation (auth, RBAC, audit, health, migrations) **AC01 spreadsheet migration**, **AC02 tagging** and **AC03 circulation**, **AC04 circulation blocks** and **AC05 inventory** and **AC06 security gate** and **AC08 reports** and **AC10 backup and restore** and **AC09 offline install, update and rollback** complete. See `docs/traceability.md` for progress per
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

## Try tagging (AC02)
Sign in as `STAFFLIB0001`. In **Catalogue** add an item (or use one you imported), open it, and click **Tag this item**. On the
**RFID tagging** page click **Read reader**, then *Generate a blank tag* (the reader is a built-in simulator; more controls are under
"Simulator controls"), then **Check** and **Link tag to item**.
The catalogue then shows the tag-to-item relationship. Use **Remove or retire tag** to take a tag off an item (a removed
tag can be reused, a retired one never can), and the **Tag registry** page to see every tag and its state; a
removed tag has a **Reuse on another item** button there.
Details and design: `docs/rfid.md`.

## Try circulation (AC03)

Sign in (`STAFFLIB0001` or `STAFFCLERK01`), open **Circulation**, pick a simulated patron card, name an item (accession
no., barcode or tag UID) and check it out, renew it and check it in. Demo members include a blocked one and one over the
fine limit, so you can see each refusal; the simulator can also make the mock library system time out or lose a reply.
Every attempt appears in the history. Late returns are charged a fine (see `docs/circulation.md`); staff can record
payments and waivers on the member panel, add members and issue or replace their cards. Details and design:
`docs/circulation.md`.

## Try inventory (AC05)

Sign in as `STAFFLIB0001`, open **Inventory**, pick a shelf location and start a session. In the simulator controls
click **Put this shelf in range**, then **Scan shelf**: the report shows found, missing, misplaced and unknown tags
with the beep and light each earned. Items need a `location` (the import has one) and tags (tag them first). Details
and design: `docs/inventory.md`.

## Try the gate (AC06)

Sign in as `STAFFADMIN01`, open **Gate events**, type a tagged book's accession number under *Simulate a walk through the
gate* and click **Walk through the gate**. Because the book was never checked out its security bit is still on, so it
raises an alarm with a CCTV snapshot and queued messages. Click **Send queued messages** to deliver them and read them
in the mock outbox. Check the book out first and walk it through again: no alarm. Details and design: `docs/gate.md`.

## Try the reports (AC08)

Sign in as `STAFFLIB0001` (or the auditor) and open **Reports**. Pick a report, set a filter (for example Circulation, outcome
Refused) and click **Apply filters**; **Download CSV** gives exactly what you see. The **Dashboard** shows 14-day charts of
completed transactions and gate alarms and the most overdue loans. Details and design: `docs/reports.md`.

## Try backup and restore (AC10)

Sign in as `STAFFADMIN01`, open **Backups**, click **Back up now**, then **Compare** (everything identical). In the
**Disaster drill** click **Run the full drill**: it damages the data, shows which tables changed, restores the backup
and shows every table identical again. The command line does the same (`python -m aisys.cli backup --help`). Details
and the restore procedure: `docs/backup-restore.md`.

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

## Try offline install, update and rollback (AC09)

`python scripts/lifecycle_demo.py --keep` builds a 0.1.0 and a 0.2.0 package (the second changes the database), installs
0.1.0 with the network deliberately broken, refuses a tampered, a foreign-signed and a downgrade package, applies 0.2.0
(backup, rehearsal on a copy, migration, start-up check, switch) and rolls back with the data proven unchanged. The
**Updates** page of an installed copy shows the versions, licence and history and can dry-run check a waiting package.
How to build, install, update, roll back and recover: `docs/deployment.md`.

## Audit log and administration

Sign in as the administrator (or the auditor for the audit log). **Audit log** filters by person, action, outcome and
text, pages through the events, and exports CSV. **Administration** adds staff, issues staff or patron cards, activates or
deactivates people and cards, unlocks locked accounts and changes roles; you cannot deactivate or demote yourself.
Details: `docs/administration.md`.

## More documentation

`docs/user-guide.md` (desk tasks), `docs/troubleshooting.md`, `docs/deployment.md` (install, update, rollback, service, TLS),
`docs/remaining-work.md` (what is left for production) and `docs/walkthrough.md` (the whole system explained).

## Loan reminders (FR09)

Patrons with an e-mail or phone are reminded a day before a loan is due and on the due day, by a loop that runs inside the app
(`auto_dispatch_seconds` in the config) or as `python -m aisys.cli notify-dispatch --every 60`. Set a member's contact on
**Circulation**; in the demo use **Due tomorrow / Due today** beside a loan to try it. Details: `docs/reminders.md`.
