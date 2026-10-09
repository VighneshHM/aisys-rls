# Assumptions, decisions and clarification log (D0/D1)

Items marked **[confirmed]** were agreed with the candidate; the rest are my own decisions to be justified in review.

| ID | Assumption / decision | Source | Rationale / consequence |
|---|---|---|---|
| A-01 | Test spreadsheet is not supplied; reviewers will bring their own. We test with a synthetic file. | confirmed | Importer must be tolerant: `.xlsx`/`.csv`, header aliases, configurable column mapping. |
| A-02 | The product is an **RFID add-on that merges with any ILMS** through a connector interface, not a replacement ILMS. A small reference mini-ILMS exists only so there is something to demo and test. | confirmed | RFID side keeps its own tables and never writes to ILMS tables; all ILMS access goes through the connector (SIP2/NCIP shaped). Satisfies NFR01 structurally. |
| A-03 | Individual recruitment task, deadline 10 Oct 2026; deliver acceptance scenarios in priority order and submit what is reached. | confirmed | Order: foundation+AC07, AC01, AC02, AC03, AC04, AC05, AC06, AC08, AC10, AC09. |
| A-04 | **Staff card** = login credential (via mock credential provider). **Patron card** = identifies a member inside a transaction only and can never create a session. | confirmed | Enforced in `auth/service.py`, DB check constraint `ck_cards_owner`, tested. |
| A-05 | Stack: Python 3.12+/FastAPI, SQLite (WAL) via SQLAlchemy + Alembic, Jinja server-rendered UI, no CDN assets. | own | Zero-install DB and no internet needs suit an isolated site. Postgres is a URL change. Trade-off: single-writer concurrency, fine for one library site prototype. |
| A-06 | Packaging: a signed ZIP with a wheelhouse and a cross-platform Python installer (`install.py`, then `manage.py`), run under the site's own service manager. | own | No Docker and no internet needed; see docs/deployment.md. (Changed from PowerShell scripts: one tool for Windows and Linux, testable.) |
| A-07 | Offline gate behaviour: middleware keeps a local cache of tag security status, evaluates gate reads while the ILMS is unreachable, queues events, replays idempotently. | own | FR07 "read the security bit while the LMS is offline". |
| A-08 | All data and credentials are synthetic. Demo password/cards are in `cli.py` and documented. | SOP stage 1 | No real personal data anywhere in the repo. |
| A-09 | Mock card reader only presents a UID; real cards would add a challenge-response `proof` (field already in the interface). | own | Provider boundary is `integrations/credentials.py`. |
| A-10 | TLS is terminated at deployment (reverse proxy or uvicorn certs); cookies are `Secure` in production. | own | "Encrypted transport where applicable" (NFR02). |
| A-11 | No NCIP/SIP2/ISO/security certification is claimed anywhere. | SOP scope | Adapters are protocol-shaped and tested against mocks only. |
| A-12 | A tag binds to an **item (physical copy)**: one active tag per item and one active item per tag, enforced in the database. A tag ends as **removed** (reusable) or **retired** (permanent). | own | See docs/rfid.md. Member-card tagging waits for member records. |
| A-13 | Devices are built from `[[devices]]` config; the mock staff reader is a simulator whose "place a tag" controls exist only outside production. | own | Hardware is out of scope (SOP section 3); a real reader is one driver class. |
| A-14 | Device write happens before the database save, with a compensating undo if the save fails. | own | A reader cannot join a DB transaction. |

## Open questions (to resolve with reviewers if possible)
1. Which ILMS (if any) will the reviewers integrate with? Connector will be written against SIP2 first.
2. Expected columns of the migration spreadsheet (importer is mapping-driven in the meantime).
3. Performance targets (we will define and publish our own, see test plan).
| A-15 | Circulation talks to the ILMS only through a connector interface; a mock stands in, worded as SIP2 or NCIP. No wire protocol is built or claimed. | confirmed | See docs/circulation.md. |
| A-16 | Fines are charged on a late return (per-day rate, grace days, cap per loan, all configurable) and kept in an append-only ledger; circulation stops when the balance exceeds a configurable limit. | own | The SOP asks only for the limit; the calculation is an extra. A real ILMS would own it. |
| A-18 | The gate decides an alarm from the security bit read at the gate, never from the ILMS; a repeat alarm for the same tag within 60 s is recorded but not re-sent. | own | See docs/gate.md. |
| A-19 | Notifications are queued first and delivered by a separate step (button, API or CLI) with retries; recipients are staff users with per-channel preferences. | own | Patron notices are a later extension of the same queue. |
| A-17 | Inventory works per location (the item's location text); items on loan and untagged items are counted, not called missing. | own | See docs/inventory.md. |
| A-20 | Report dates are whole days in UTC; CSV exports follow the on-screen filters and neutralise spreadsheet formulas. | own | See docs/reports.md. |
| A-21 | A restore replaces all current data (sessions and audit log up to the backup's moment included) after saving a safety copy, and is confirmed by typing RESTORE. | own | See docs/backup-restore.md. |
| A-25 | A patron is reminded once a day before a loan is due and once on the due day, by e-mail and/or SMS to the details staff entered; reminders are queued once per loan, kind, channel and due date and delivered by a loop in the app. | confirmed | See docs/reminders.md. Overdue notices are not part of it. |
| A-22 | An update is applied with the application stopped; it rehearses on a copy of the data, takes a verified backup, and switches a pointer last. A rollback restores that backup only when the update changed the database layout. | own | See docs/deployment.md. |
| A-23 | Update packages are signed (Ed25519) and the installation pins the key it was installed with; offline activation is a signed licence file checked locally. | own | No certificate authority or revocation; the demo keys and licence are synthetic. |
| A-24 | Applying and rolling back are command-line only; the Updates page reads, lists history and dry-run checks a package. | own | They restart the app. |
| A-26 | Search is full-text over title, author, ISBN, publisher, accession number, barcode, call number and shelf (SQLite FTS5), kept current by database triggers; "contains" matching is the fallback when the index finds nothing. | own | See docs/architecture.md section 5. |
| A-27 | The public catalogue (OPAC) is open without sign-in and shows only titles, shelves and availability; it can be switched off. | own | docs/security-checks.md. |
| A-28 | Net cataloguing looks a book up by ISBN in an outside catalogue behind an interface; the only source here is a mock with synthetic records. A person checks and saves the result. | own | Same pattern as the other adapters. |
| A-29 | Acquisitions and serials are reference-level workflows (orders, receiving, subscriptions, late issues), not a full acquisitions module (no budgets, invoices or claims letters). | own | FR01 is satisfied for an add-on (A-02). |
| A-30 | Barcodes are Code 128 (subset B); spine labels split the call number into short lines; labels are printed from the browser or queued as a text job for a label printer. | own | docs/use-cases.md UC-12. |
| A-31 | A patron card is personalised on a card-printer device (a mock here) and the printer also writes the card number to the chip. | own | docs/architecture.md section 6. |
| A-32 | Encrypted transport is either the application's own HTTPS (certificate and key in the configuration) or a TLS proxy in front; both are supported and the first is tested. | own | docs/deployment.md. |
