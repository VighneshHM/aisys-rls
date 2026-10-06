# Assumptions, decisions and clarification log (D0/D1)

Items marked **[confirmed]** were agreed with the candidate; the rest are my own decisions to be justified in review.

| ID | Assumption / decision | Source | Rationale / consequence |
|---|---|---|---|
| A-01 | Test spreadsheet is not supplied; reviewers will bring their own. We test with a synthetic file. | confirmed | Importer must be tolerant: `.xlsx`/`.csv`, header aliases, configurable column mapping. |
| A-02 | The product is an **RFID add-on that merges with any ILMS** through a connector interface, not a replacement ILMS. A small reference mini-ILMS exists only so there is something to demo and test. | confirmed | RFID side keeps its own tables and never writes to ILMS tables; all ILMS access goes through the connector (SIP2/NCIP shaped). Satisfies NFR01 structurally. |
| A-03 | Individual recruitment task, deadline 10 Oct 2026; deliver acceptance scenarios in priority order and submit what is reached. | confirmed | Order: foundation+AC07, AC01, AC02, AC03, AC04, AC05, AC06, AC08, AC10, AC09. |
| A-04 | **Staff card** = login credential (via mock credential provider). **Patron card** = identifies a member inside a transaction only and can never create a session. | confirmed | Enforced in `auth/service.py`, DB check constraint `ck_cards_owner`, tested. |
| A-05 | Stack: Python 3.12+/FastAPI, SQLite (WAL) via SQLAlchemy + Alembic, Jinja server-rendered UI, no CDN assets. | own | Zero-install DB and no internet needs suit an isolated site. Postgres is a URL change. Trade-off: single-writer concurrency, fine for one library site prototype. |
| A-06 | Packaging: ZIP with wheelhouse + PowerShell install/update/rollback scripts, run as a Windows service. | own | Works on Windows Server 2022 without Docker. |
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
| A-16 | Fines are a stored amount per member; circulation stops when it exceeds a configurable limit. Overdue fines are not calculated. | own | Reasonable for the exercise; real fines come from the ILMS. |
