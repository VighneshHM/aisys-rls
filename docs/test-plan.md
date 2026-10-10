# Test plan (D6)

## 1. Purpose and scope

Show, with evidence that can be re-run, that the delivered add-on meets the requirements in `docs/requirements.csv`: the ten
acceptance scenarios (AC01 to AC10), the functional requirements (FR01 to FR12) and the non-functional requirements (NFR01 to
NFR09). In scope: the web application, its API, the RFID middleware through mock devices, the ILMS connector through a mock, the
migration utility, backup and restore, the offline lifecycle tools, the generated documents. Out of scope (SOP section 3): real
hardware, a live ILMS or mail server, certification of any protocol.

## 2. Test levels and types

| Type (NFR05) | What is tested | How | Evidence |
|---|---|---|---|
| Functional | every rule of every feature, including refusals and edge cases | pytest against the real application (FastAPI test client, real database file per test) | 29 test files; list below |
| Integration | routes + services + database + mock devices together; the connector with retries and timeouts; delivery through provider adapters; a real HTTPS server; a real background loop; real subprocess installs and updates | pytest (some tests start a real server, a real `uvicorn`, or a real virtual environment) | `test_gate`, `test_circulation`, `test_security_ops`, `test_lifecycle`, `test_reminders` |
| Migration | 200- and 20,000-row files, duplicates, invalid rows, reconciliation, rollback, restore after a failed import | pytest with generated synthetic files | `test_migration`, `test_backup` |
| Performance | search, reports, import, backup, restore, reminders and concurrent desks with 20,000 items | `scripts/perf_report.py` | `docs/performance-results.md` |
| Security | permissions, hostile input, injection, path traversal, TLS, headers, masking, signed packages, dependency audit | pytest, `ruff` (Bandit rules), `pip-audit`, probe scripts | `docs/security-checks.md` |
| Regression | the whole suite on every pull request, on three Windows images | GitHub Actions | CI history |
| Backup, restore, offline update, rollback | proving the original data is intact | pytest and `scripts/lifecycle_demo.py` (network blocked and logged) | `test_backup`, `test_lifecycle`, demo output |
| User acceptance | the scenarios a library person performs, in the browser | the scripted scenarios in `docs/uat.md` | `docs/uat.md` |
| Usability and layout | pages at desktop and phone width, dark and light themes, keyboard use, empty states, error messages | browser checks recorded in `docs/uat.md`; asset checks in `test_ui_assets` | `docs/uat.md` |

## 3. Environments and data

* **Development machine**: Windows 11, Python 3.13, the project's virtual environment.
* **CI**: GitHub-hosted Windows runners (`windows-latest`: Python 3.12 and 3.13; `windows-2022`: Python 3.12).
* **Data**: synthetic only. Tests build their own data; the sample spreadsheets (`samples/`, 200 and 20,000 rows, with a manifest of
  the expected valid, invalid and duplicate counts) come from a seeded generator, so results repeat. Demo people and cards come
  from `seed-demo --minimal`; the full demo library (`seed-demo`, `docs/demo-data.md`) has its own tests.
* **Isolation**: every test gets a fresh temporary database and backup folder (`tests/conftest.py`); tests never touch
  `data/`.

## 4. Entry and exit criteria

* **Entry for a pull request**: the branch is up to date with `main`, formatted and lint-clean locally.
* **Exit (merge)**: formatter, linter, all tests and the dependency scan pass in CI; the requirement, design document and
  traceability matrix are updated; any defect found while testing is fixed or logged.
* **Exit (release)**: the whole suite passes on the final `main`; `scripts/lifecycle_demo.py` passes with the network blocked (every connection off the machine refused and logged; the log must stay empty);
  `docs/uat.md` has no open failure; `docs/defect-log.md` has no open defect of high severity; the evaluation tag is created.

## 5. Traceability

Every test is tagged with the requirement it proves. `scripts/trace.py` generates `docs/traceability.md` (requirement, status,
number of tests, and the test names); `tests/test_docs.py` fails if it is stale or if a requirement marked *done* has no test.

## 6. Tests by area

| File | Tests | Covers |
|---|---|---|
| `test_foundation.py` | 23 | sign-in (card and password), lockout, roles, audit log, health, migrations up and down |
| `test_migration.py` | 31 | AC01: staging, mapping, validation rules, duplicates, reconciliation, rollback, the 20,000-row run |
| `test_tagging.py`, `test_tag_lifecycle.py` | 37, 26 | AC02/FR05: validate, link, re-tag, remove, retire, monitoring, database invariants |
| `test_circulation.py`, `test_fines.py`, `test_members.py`, `test_member_cards.py` | 22, 8, 8, 11 | AC03/AC04/FR04/FR05: check-out, renew, check-in, blocks, fines ledger, members, cards and the member-record check |
| `test_inventory.py` | 11 | AC05/FR06: sweeps, signals, missing and misplaced, closing |
| `test_gate.py` | 32 | AC06/FR07/FR09: alarms, offline behaviour, duplicates, CCTV, queued delivery, retries, the production gate loop, `gate-poll`, audited notification actions, a driver or camera that fails in any way |
| `test_reports.py` | 18 | AC08/FR08: reports, filters, dashboards, CSV, hostile input, scale |
| `test_create_admin.py` | 15 | NFR04: the first administrator of a real install (sign-in, audit, weak password, bad or taken name, no prompt without a terminal) |
| `test_backup.py` | 25 | AC10/NFR01: backups, proof, restore, busy database, maintenance mode, hostile names |
| `test_lifecycle.py`, `test_lifecycle_api.py` | 49, 12 | AC09/FR12: signatures, tampering, install, update, failure paths, rollback, recovery |
| `test_admin.py` | 26 | FR11: audit log, staff and cards, lock-out rules, custom roles, no granting beyond your own rights |
| `test_reminders.py` | 42 | FR09: reminders, once-only, cancellation, the loop, contact details |
| `test_search_shelves.py` | 73 | FR01/FR02/FR06: full-text search, public catalogue, shelves, order check, net cataloguing, public search counts and pages every match |
| `test_acquisitions_serials_labels.py` | 63 | FR01/FR04: orders, serials, barcodes and labels, recent label print jobs, card printing |
| `test_member_admin.py` | 36 | FR04/FR11: the Patrons page: list, search, filters, remove (refused with items out or a fine owed), restore, erase, permissions, no names in the append-only records, the migration |
| `test_dashboard_counts.py` | 3 | AC08/FR04: the dashboard count tiles (patrons, blocked, owing, items out, untagged, gate, messages) per role |
| `test_demo_seed.py` | 25 | D3/D9: the full demo library: the numbers asked for (7 staff, 20 patrons, 250-300 books, the 200-row import all tagged, a few untagged), patrons in every situation with past and current loans, every menu has data, every page opens, only into an empty catalogue, `--minimal` |
| `test_opac_session.py` | 22 | NFR02/FR02: not in the staff menu, reached from the sign-in page; the warning's sign-out (POST, audited, cross-site refused); opening the public catalogue signs nobody out; kiosk host names; no cached pages; scrollable menu that remembers its position |
| `test_catalogue_edit.py` | 11 | FR01: corrections and copy status with the importer's rules, on-loan guard, shared-record count, audit trail, no deletes, edits that would lose a value refused |
| `test_settings.py` | 37 | FR11: the Settings page and API, validation, audit, undo, restart |
| `test_sip2_codec.py` | 70 | FR03: SIP2 message encoding and decoding, checksum, sequence, limits, hostile and fuzzed input |
| `test_sip2.py` | 62 | FR03/AC03: SIP2 client and connector against the simulator over a real socket, including the web API end to end; lost, dropped and damaged answers; verify-before-resend; error mapping; configuration and password handling |
| `test_security_ops.py`, `test_config.py` | 13, 9 | NFR02/NFR03/NFR06: masking, headers, HTTPS, doctor, configuration, NUL text and loose numbers refused |
| `test_ui_assets.py`, `test_docs.py` | 32, 11 | page assets and scripts, generated documents, SBOM and licence inventory, hashed lock files, pinned build backend, redistribution notes, stage timeline, sample reports, deliverables present |

Total: 864 test cases (some are parametrised).

## 7. Manual scenarios

The manual scenarios are the UAT script in `docs/uat.md` (one per use case in `docs/use-cases.md`), each with steps, expected
result and the recorded outcome. They are run in a browser against a freshly seeded database and repeat with
`scripts/lifecycle_demo.py` for the install, update and rollback scenario.

## 8. Defect handling

Found defects are logged in `docs/defect-log.md` (severity, steps, owner, status, fix version, retest evidence). Every fixed defect
gets a regression test where one is practical, in the same pull request. Severity: **high** (data loss or corruption, security
exposure, a scenario cannot be completed), **medium** (wrong result with a workaround, a feature degraded), **low** (wording,
layout, convenience).

## 9. Risks and limits of the evidence

* Devices, ILMS, mail, SMS and cameras are mocks, so no test proves a vendor integration (R-05 in `docs/risks.md`).
* The browser scenarios were run by the development team, not by library staff; a site acceptance test with real users and
  devices is the first production task (`docs/remaining-work.md`).
* Only Windows has been exercised. Linux is expected to work and is not verified.
