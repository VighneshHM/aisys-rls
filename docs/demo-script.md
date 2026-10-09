# Demonstration script and presentation (D9)

A 30-minute live demonstration that walks through the ten acceptance scenarios in the SOP's order, then a short presentation
outline and the answers to the questions a reviewer is likely to ask. Everything shown is synthetic.

## Before you start (5 minutes, once)

```bash
python -m pip install -e ".[dev]"
python -m aisys.cli seed-demo            # database, four staff, six patrons, contact details, alert settings
python -m aisys.cli run                  # http://127.0.0.1:8000  (simulators are on in development)
```

Have ready: `samples/library_200.xlsx` and `samples/library_20000.xlsx`; a second terminal for the lifecycle demo; the sign-in page
open. Sign in by choosing a simulated staff card on the sign-in page (administrator `STAFFADMIN01`, librarian `STAFFLIB0001`,
desk clerk `STAFFCLERK01`, auditor `STAFFAUDIT01`).

## The run (about 30 minutes)

| Min | Scenario | Do | Say / show |
|---|---|---|---|
| 0 | Scope | Show `docs/index.md` | An RFID add-on that joins an existing ILMS through connector interfaces; every device and service is mocked behind an interface; all data is synthetic |
| 2 | **AC07** card sign-in | Sign in as the librarian; try the patron card; sign in as the clerk and show the shorter menu | Staff card = login; patron card never logs in; menus follow the role |
| 4 | **AC01** import | Librarian, **Data migration**, upload `library_200.xlsx`; show problems, duplicates; **Commit**; show the reconciliation; **Roll back** | Staged first, never touches existing records, counts reconcile, rollback by batch. Mention the 20,000-row run: 13 s validate, 7 s commit |
| 8 | **AC02** tag a book | **Catalogue**, **Add item**, ISBN `9780001000032`, **Look up the ISBN**, save; **Tag this item**, read the reader, generate, **Check**, **Link** | Net cataloguing fills the form; validation is the same as import; the tag-to-item link is shown |
| 11 | **AC03/AC04** circulation | Clerk, **Circulation**, Priya (`PATRON000001`), check out, renew, check in; then the blocked member and the member over the limit; a reference item | Goes through the ILMS connector (the mock by default; SIP2 is shown in the extras) with retries; every action is in the history; refusals say why |
| 15 | **AC05** inventory | Librarian, **Inventory**, a shelf, simulate a handheld with missing and stray books, scan, close; then **Shelves** to see the books out of order | Beep and light per tag; missing, misplaced, unknown; the virtual bookshelf outlines books to move |
| 19 | **AC06** gate | **Gate events**: walk an unissued book through; open the event; **Send queued messages** | Alarm from the security bit (works while the ILMS is down), accession number, camera snapshot, queued e-mail with retries |
| 22 | **AC08** reports | Dashboard and **Reports** with filters; export CSV | Seven reports, charts, CSV that cannot run formulas |
| 24 | **AC10** restore | Administrator, **Backups**, **Run the full drill**; then arm a failing import and commit one; **Compare** | A damaging change, then restore, then every table identical; a failed import leaves library data identical to the backup |
| 27 | **AC09** offline lifecycle | Second terminal: `python scripts/lifecycle_demo.py`; then open **Updates** | Install, refuse a tampered, a foreign-signed and an older package, update, roll back with the data proven; no network used |
| 30 | Wrap-up | **Audit log** | Every action recorded with who, what, when; card numbers masked |

Extras if there is time (each 2 minutes): **SIP2** (see below); the public catalogue `/opac` (signed out, search, **Show the shelf**); **Labels** (spine and
barcode); **Acquisitions** (order, place, book in); **Serials** (late issue, claim); **Print the card**; patron reminders
(**Due tomorrow** beside a loan, watch the message appear); **Administration** (deactivate someone, try to deactivate yourself); **Settings** (as administrator, change the loan period or fine per day, see it apply at once, find the `admin.settings.update` line in the **Audit log**, then **Use file value**).

### The SIP2 extra (2 minutes)

Say first: this is a SIP2 client talking to a **simulator** written from the specification, not to a real library system.

1. Terminal A: `python -m aisys.cli sip2-sim --port 6101 --user aisys --password demo`
2. Terminal B: set `$env:AISYS_ILMS_PASSWORD = "demo"`; in `config/config.toml` set `[ilms]` to `driver = "sip2"`, `endpoint = "127.0.0.1"`,
   `port = 6101`, `login = "aisys"`; run `python -m aisys.cli doctor` and point at the ILMS line (answered and online).
3. Start the application (`python -m aisys.cli run`) in terminal B, sign in as the clerk, and check out a book to Priya. Show the history line.
4. Stop the simulator and try a check-in: the clerk gets a clear "not answering" message and nothing is half-recorded. Restart the simulator
   and repeat.

Put `driver = "mock"` back afterwards. Details and limits: `docs/sip2.md`.

## If something goes wrong on the day

| Symptom | Quick fix |
|---|---|
| Sign-in page shows no simulated cards | The database was not seeded: stop, `python -m aisys.cli seed-demo`, run again |
| A page looks old after a code change | Restart the application (the server keeps the old code) |
| "Stale" or missing fields on the member panel | Same: restart |
| A step fails | Say so, show the audit log line and `docs/troubleshooting.md`; every failure path has a message and a test |

## Presentation outline (12 slides, 15 minutes)

The deck is `docs/presentation.html`: one self-contained file (no network), open it in a browser; arrow keys or a click move between slides, and each slide has speaker notes. It follows this outline.

1. **The problem and the scope**: add RFID to an existing library system without touching its records; what is mocked and why.
2. **Architecture**: the layered diagram (`docs/architecture.md` section 2) and the interface/mocks rule.
3. **What was built**: the ten scenarios as a checklist, with the requirement coverage (all 31 requirements done; FR03 rests on a SIP2 client tested only against a simulator, with NCIP 2.0 not built, and says so).
4. **Data safety (NFR01)**: staging, backup before every change, per-table proof, restore with a safety copy.
5. **Integration**: the connector with retries and the same transaction id; devices behind Protocols; notifications queued first.
6. **Security**: roles and permissions, signed packages, TLS, masked logs, hostile-input tests, dependency audit.
7. **Offline lifecycle**: install, update with a rehearsal, rollback; the SBOM and licences.
8. **Search and shelves**: full-text index kept current by triggers; the public catalogue; the virtual bookshelf.
9. **Testing**: 745 test cases mapped to requirements, three Windows CI images, performance at 20,000 items, the defect log.
10. **What the testing found**: examples (the search that took 1.3 s, duplicate delivery, the restore race) and how they were fixed.
11. **Known limits and production backlog**: `docs/remaining-work.md` (no real hardware, SIP2 tested only against a simulator and no NCIP, SQLite only, no pen test).
12. **Questions and the live demonstration.**

## Questions to expect

| Question | Answer |
|---|---|
| Is SIP2 or NCIP really implemented? | SIP2: yes, a subset of 3M SIP 2.00 over TCP (login, status, patron status and information, checkout, checkin, renew, item information, resend), with checksums and verify-before-resend. It is tested only against a simulator written from the specification: not certified, not tried against any vendor ILMS, and SIP2 itself is unencrypted so a tunnel is needed. NCIP 2.0: no, not built (`docs/sip2.md`) |
| Can a real reader be plugged in? | Yes: one class implementing the device `Protocol`, registered by name, selected in `[[devices]]`; nothing else changes |
| What stops a bad import corrupting the library? | Staging first, insert-only commit in one transaction, a verified backup before it, rollback by batch, and the table-by-table proof |
| What happens if an update fails halfway? | The data is restored from the pre-update backup, the new release is removed and the old version stays current; `manage.py recover` handles a crash |
| Why SQLite? | Zero-install and offline-friendly for an isolated site, with FTS5 built in; PostgreSQL is a documented future option; the limit is one writer at a time |
| Is it production-ready? | It is a tested prototype. `docs/remaining-work.md` lists what production needs: real drivers, a site acceptance test, a server-class soak test, TLS in front |
