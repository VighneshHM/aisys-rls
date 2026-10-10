# AISYS RFID Library

RFID-enabled library add-on that integrates with an existing ILMS through connector interfaces, with mock adapters for
every device and service that is unavailable. Built for the AISYS Candidate Software Development SOP.

**Status:** every SOP requirement (FR01 to FR12, NFR01 to NFR09) and all ten acceptance scenarios (AC01 to AC10) are
implemented and tested: spreadsheet migration, tagging, circulation through a mock ILMS or the **SIP2** protocol, blocks and
fines, inventory, the security gate, notifications and patron reminders, reports and dashboard, full-text search and the public
catalogue, shelves, acquisitions, serials, labels and cards, staff, **patrons**, **roles** and **Settings** administration, backup and restore,
and the signed offline install, update and rollback. NCIP 2.0 is not built (SIP2 meets the protocol requirement, decision DR-13).
`docs/traceability.md` maps every requirement to its tests, `docs/assumptions.md` and `docs/decisions.md` record the decisions,
and `docs/remaining-work.md` lists what is left for a live site.

Contents: [Install at a library](#install-at-a-library-offline) · [Try it on one computer](#try-it-on-one-computer-windows-powershell) ·
[Try each scenario](#try-the-spreadsheet-import-ac01) · [Features](#correct-a-record-withdraw-a-copy-fr01) ·
[Configuration](#configuration) · [Develop](#develop) · [Layout](#layout) · [More documentation](#more-documentation)

## Install at a library (offline)

A library receives one **signed package** (`aisys-<version>.zip`), a **licence file** (`licence.json`) and the vendor's
**public key** (`vendor.pub`, which should arrive separately from the package). The package holds the application and every
dependency, so the server needs **no internet**. Full procedure, checklist and recovery: `docs/deployment.md`.

**Before you start** (the server): Windows 11 or Windows Server 2022 or later, **Python 3.12 or newer** (the only thing installed
separately), 500 MB free disk, and a short install path such as `C:\aisys` (Windows stops at 260 characters). Unzip the package
to a folder, for example `C:\aisys-package`.

1. **Check Python and disk.** `python --version` shows 3.12 or newer and the drive has the free space. The full
   environment checklist is in `docs/deployment.md`; the installer also refuses early if Python, disk or the path length are wrong.
2. **Install.**

   ```powershell
   cd C:\aisys-package
   python install.py --root C:\aisys --licence licence.json --trusted-key vendor.pub
   ```

   It verifies the signature and every file, checks the licence offline, builds a private environment from the package's own
   wheels, prepares the database, starts the application once to prove it runs, and only then records the install. If anything
   fails the half-built release is removed and nothing is left behind.
3. **Configure.** Edit `C:\aisys\config\config.toml` (copied from the sample at install): set `[app] environment = "production"`,
   the `[ilms]` connector (`mock`, or `sip2` with the library system's host, port and login) and the device drivers. The
   secret key is generated for you in `config\secret.key`. Put the SIP2 password in the environment variable
   `AISYS_ILMS_PASSWORD`, never in the file. Every setting is in the table under *Configuration reference* in `docs/deployment.md`.
4. **Check, then start.** `python C:\aisys\manage.py doctor` and fix every FAIL (Python, SQLite, disk, folders, port, secret key,
   the ILMS link; it changes nothing). Then `python C:\aisys\manage.py run` (http://127.0.0.1:8000). For everyday use run it under a service manager
   (NSSM or Task Scheduler on Windows) and put an HTTPS proxy in front: examples are in `docs/deployment.md`, *Running in production*.
5. **Create the first administrator.** A new install has no staff, so make one (it asks for the password twice and never
   shows it; at least 12 characters):

   ```powershell
   python C:\aisys\manage.py create-admin --username site.admin --name "Site Admin"
   ```

   Sign in on the sign-in page with that username and password. (`--seed-demo` at install loads the synthetic demo library
   and a published demo password: for a trial or training copy only, never live data.)
6. **Set up.** The administrator adds staff and cards on **Administration**, then loads the catalogue with
   **Data migration** (`docs/migration.md`) and tags items (`docs/rfid.md`). Back up from **Backups** and copy `data\backups\`
   off the server (`docs/backup-restore.md`).

**Update** (the new package goes in `C:\aisys\incoming\`; stop the application first):

```powershell
python C:\aisys\manage.py update aisys-0.2.0.zip --dry-run     # check only, changes nothing
python C:\aisys\manage.py update aisys-0.2.0.zip
```

It backs up the data, installs the new version beside the old one, rehearses the migration on a copy, then switches. Any failure
before the switch leaves the old version and its data as they were.

**Roll back** to the previous version (the data comes back from the pre-update backup when the update changed the database layout):

```powershell
python C:\aisys\manage.py rollback --dry-run
python C:\aisys\manage.py rollback --yes
```

Other commands: `manage.py status` (versions, licence, history), `manage.py recover` (after a power cut mid-update),
`manage.py prune` (free disk).

**For the vendor** (a build machine with the development tools and internet once): `python scripts\release.py keygen`,
`licence` and `build` make the key pair, a customer's licence and the signed package. See *Build a package* in `docs/deployment.md`.
To watch the whole install, update and rollback with the network blocked, see [AC09](#try-offline-install-update-and-rollback-ac09).

## Try it on one computer (Windows, PowerShell)

For evaluation and development. This runs from the source folder, not as an installed copy.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements.lock -r requirements-dev.lock
.\.venv\Scripts\python -m pip install -e . --no-deps --no-build-isolation
.\.venv\Scripts\aisys seed-demo     # migrates the DB and loads the SYNTHETIC demo library
.\.venv\Scripts\aisys run           # http://127.0.0.1:8000  (API docs at /docs)
```

The demo library has 7 staff, 20 patrons in different situations, 259 books (191 imported from the sample spreadsheet
and tagged, 15 more left untagged) and about a hundred days of activity, so every menu has something in it: see
`docs/demo-data.md`. `seed-demo --minimal` loads only the staff and patron cards with an empty catalogue, which is
where the guided tour and the import below start from.

Demo staff card UIDs (synthetic), one of each role on the sign-in page: `STAFFADMIN01`, `STAFFLIB0001`, `STAFFCLERK01`,
`STAFFAUDIT01`; the other staff (`STAFFLIB0002`, `STAFFCLERK02`, `STAFFCLERK03`) can be typed in the card box.
Patron card `PATRON000001` is rejected at login by design. Demo admin password: see `src/aisys/cli.py`.
`docs/demo-script.md` is a guided tour.

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
fine limit, so you can see each refusal; the simulator can also make the mock library system time out or lose a reply
(`[ilms] driver = "sip2"` uses the SIP2 protocol instead; see "SIP2 and the simulator" below).
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
in the mock outbox. Check the book out first and walk it through again: no alarm. In production a real gate is read by the
gate loop (`[gate] poll_seconds`), by `python -m aisys.cli gate-poll --every 2`, or by **Read the gates now**. Details and
design: `docs/gate.md`.

## Try the reports (AC08)

Sign in as `STAFFLIB0001` (or the auditor) and open **Reports**. Pick a report, set a filter (for example Circulation, outcome
Refused) and click **Apply filters**; **Download CSV** gives exactly what you see. The **Dashboard** shows 14-day charts of
completed transactions and gate alarms and the most overdue loans. Details and design: `docs/reports.md`.

## Try offline install, update and rollback (AC09)

To install at a library, follow [Install at a library](#install-at-a-library-offline). To see the whole lifecycle proven in
one command, run `python scripts/lifecycle_demo.py --keep`: it builds a 0.1.0 and a 0.2.0 package (the second changes the
database), installs 0.1.0 with the network blocked (every connection off the machine is refused and logged), refuses a
tampered, a foreign-signed and a downgrade package, applies 0.2.0 (backup, rehearsal on a copy, migration, start-up check,
switch) and rolls back with the data proven unchanged. The **Updates** page of an installed copy shows the versions,
licence and history and can dry-run check a waiting package. How to build, install, update, roll back and recover:
`docs/deployment.md`.

## Try backup and restore (AC10)

Sign in as `STAFFADMIN01`, open **Backups**, click **Back up now**, then **Compare** (everything identical). In the
**Disaster drill** click **Run the full drill**: it damages the data, shows which tables changed, restores the backup
and shows every table identical again. The command line does the same (`python -m aisys.cli backup --help`). Details
and the restore procedure: `docs/backup-restore.md`.

## Correct a record, withdraw a copy (FR01)

In **Catalogue** open an item and press **Edit details or status**: fix any field (the accession number is fixed), or mark the copy
damaged, lost or withdrawn with a reason. The same rules as a new item apply, every change is audited, and nothing is deleted.

## Search, shelves, orders, serials, labels, cards (FR01, FR02, FR04, FR06)

* **Search**: the catalogue search is full-text (try `dispos` for *The Dispossessed*); the **public catalogue** at `/opac` needs no
  sign-in and shows titles, shelves and availability only.
* **Shelves** shows a shelf as a row of books in call-number order; after an inventory sweep, books out of order are outlined.
* **Add item** has **Look up the ISBN** (net cataloguing; try `9780001000032`).
* **Acquisitions**, **Serials** and **Labels** are in the menu for librarians; **Print the card** is under a member on Circulation.
* `python -m aisys.cli doctor` checks the computer and configuration. `python scripts/perf_report.py` measures performance at 20,000 items.

## Patrons (FR04)

**Patrons** lists every member with search and filters (blocked, items out, owe a fine, removed). Librarians and administrators
can **remove** a member who has left (history is kept, the card stops working; refused while items are out or a fine is owed) and
**restore** them; administrators can also **erase** the personal details of a removed member. Details: `docs/administration.md`
section 3a.

## Loan reminders (FR09)

Patrons with an e-mail or phone are reminded a day before a loan is due and on the due day, by a loop that runs inside the app
(`auto_dispatch_seconds` in the config) or as `python -m aisys.cli notify-dispatch --every 60`. Set a member's contact on
**Circulation**; in the demo use **Due tomorrow / Due today** beside a loan to try it. Details: `docs/reminders.md`.

## SIP2 and the simulator (FR03)

The library-system connector can speak the SIP2 protocol (3M 2.00) over TCP: set `[ilms] driver = "sip2"` with `endpoint`, `port`,
`login`, `institution` and `location` in `config/config.toml`, and put the login password in the environment variable
`AISYS_ILMS_PASSWORD` (never in the file). To try it without a real library system, run the simulator and point the application at it:

```powershell
python -m aisys.cli sip2-sim --port 6101 --user aisys --password demo     # terminal 1
$env:AISYS_ILMS_PASSWORD = "demo"                                         # terminal 2, then set [ilms] to
                                                                          # driver sip2, endpoint 127.0.0.1, port 6101, login aisys
python -m aisys.cli doctor                                                # the ILMS line should be OK
python -m aisys.cli run
```

Honest limits: tested only against the simulator (written from the specification), not certified and not tried on any vendor
system; a subset of SIP2; no encryption in SIP2 itself (use a VPN, stunnel or TLS proxy). NCIP 2.0 is not implemented: SIP2
meets the SOP's protocol requirement (decision DR-13), and an NCIP connector would be one more class behind the same boundary.
Details: `docs/sip2.md`.

## The public catalogue and public terminals

The public catalogue is linked from the sign-in page, not the staff menu: on a shared screen, sign out first, then open it.
Opening `/opac` by its address changes nothing (it warns if you are still signed in and offers a sign-out). A public terminal gets its own host name in
`[opac] kiosk_hosts`, where only the catalogue exists (`docs/deployment.md`, *Public terminals*).

## Audit log and administration

Sign in as the administrator (or the auditor for the audit log). **Audit log** filters by person, action, outcome and
text, pages through the events, and exports CSV. **Administration** adds staff, issues staff or patron cards, activates or
deactivates people and cards, unlocks locked accounts and changes roles; you cannot deactivate or demote yourself.
**Roles** lists the four built-in roles and lets an administrator add custom ones with a chosen set of permissions,
change them, and remove them when nobody holds them.
Details: `docs/administration.md`.

## Settings (FR11)

Administrators open **Settings** to change the library's rules in the browser: loan period, fines, reminders, sign-in limits and the
public catalogue switch. Changes apply at once, are audited, and **Use file value** goes back to the configuration file. Devices,
drivers and certificates stay in `config/config.toml` (see `docs/administration.md`).

## Configuration
Copy `config/config.sample.toml` to `config/config.toml` (an installed copy already has one). Secrets come from the environment only
(`AISYS_SECRET_KEY`, required in production; the installer generates one). Override paths with `AISYS_CONFIG`, `AISYS_DATABASE_URL`,
`AISYS_ENV`. Every key and variable is listed in `docs/deployment.md`.

## Develop

The same gates as CI (`.github/workflows/ci.yml`):

```powershell
.\.venv\Scripts\python -m ruff format --check .   # formatting
.\.venv\Scripts\python -m ruff check .            # lint and static analysis (with the Bandit security rules)
.\.venv\Scripts\python -m mypy                    # type check
.\.venv\Scripts\python -m pytest -n auto          # tests, in parallel (real migrations, temporary databases)
.\.venv\Scripts\python scripts\lifecycle_demo.py  # real offline install, update and rollback with the network blocked
```

Generated documents (the tests fail when one is stale): `scripts\trace.py` (traceability matrix), `scripts\gen_docs.py` (data
dictionary and API reference), `scripts\sbom_lock.py` (SBOM and licence inventory) and `scripts\sample_reports.py` (sample
migration reports). Dependencies are pinned with hashes in `requirements.lock` (runtime, shipped) and `requirements-dev.lock`
(development and build tools, never shipped).

## Layout
```
src/aisys/
  app.py, routes.py, deps.py      web app factory, sign-in and system API, auth/RBAC dependencies
  *_routes.py, pages.py           HTTP API per area, and the staff pages
  auth/                           password hashing, login service, permission catalogue and built-in roles
  catalogue/, labels/, shelves/   items, full-text search (FTS5), barcodes and spine labels, shelves and the virtual bookshelf
  acquisitions/, serials/         purchase orders and receiving, subscriptions and issues
  circulation/                    loans, fines, members and their removal; the ILMS connector (mock, and sip2/: client, codec, simulator)
  rfid/                           device-neutral interfaces, mocks, registry, tagging (item and member validation)
  inventory/, gate/               shelf sweeps; gate events, footfall and camera
  notifications/, reports/        queue, providers, reminders; the seven reports and the dashboard
  migration/, backup.py           spreadsheet import with staging and rollback; verified backup and restore
  integrations/                   smart-card credential provider and net cataloguing (mocks)
  settings_admin.py, doctor.py    the Settings page rules; the environment and configuration check
  models.py, db.py, migrations/   ORM, engine, versioned Alembic migrations (0001 to 0012)
  audit.py, logging_setup.py      append-only audit trail, masked structured logs
  web/                            templates and static files (no external requests)
src/aisys_lifecycle/              offline packages: signing, licence, install, update, rollback, SBOM
scripts/                          release, lifecycle demo, generators for the generated documents, performance report
docs/                             every SOP deliverable; docs/index.md maps them
tests/                            pytest suite, tagged with requirement IDs
```

## More documentation

`docs/index.md` (every document against the SOP deliverable it satisfies), `docs/user-guide.md` (desk tasks), `docs/troubleshooting.md`,
`docs/deployment.md` (install, update, rollback, service, TLS), `docs/administration.md`, `docs/sip2.md` (the SIP2 connector),
`docs/remaining-work.md` (what is left for production) and `docs/walkthrough.md` (the whole system explained).
