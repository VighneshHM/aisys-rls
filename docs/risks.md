# Risk register (Stage 1, D0)

Likelihood and impact are Low / Medium / High / Critical, judged by the project owner with the AI assistant. **Owner** is
VighneshHM unless the risk can only be closed by someone at the library, in which case it names the role. **Status**:
**Mitigated** (control built and tested), **Accepted** (known, not mitigated here, stated in the documents), **Open** (needs action
by the named owner), **Closed** (cannot occur any more). This register is reviewed when a deliverable is finished; the last review
was on 2026-10-09. Nothing was penetration tested or run against real hardware, so "Mitigated" means mitigated in the prototype.

| ID | Risk | Category | Likelihood | Impact | Mitigation | Owner | Status |
|---|---|---|---|---|---|---|---|
| R-01 | Five-day deadline cannot fit every acceptance scenario | schedule | High | High | Priority order (A-03); each stage shipped tests and docs so partial delivery stayed coherent. All ten scenarios were reached | VighneshHM | Closed |
| R-02 | Reviewers' spreadsheet has an unexpected layout | data | High | Medium | Mapping-driven importer, header aliases, per-row error log, dry-run before commit | VighneshHM | Mitigated |
| R-03 | Migration damages existing data | data | Low | Critical | Staging area, single-transaction commit with batch ID, rollback by batch, verified backup before import (NFR01) | VighneshHM | Mitigated |
| R-04 | Offline update or rollback breaks the database schema | technical | Medium | High | Rehearsal on a copy of the data, verified backup before the real migration, automatic restore on failure, pointer switched last (`docs/deployment.md`) | VighneshHM | Mitigated |
| R-05 | Mock-only integration hides real-vendor issues | technical | Medium | Medium | Narrow connector and device interfaces; the limitation is stated; no certification is claimed. Real hardware is untested (see R-17) | VighneshHM | Accepted |
| R-06 | SQLite write contention under gate or inventory bursts | technical | Low | Medium | WAL and busy timeout; the event queue serialises writes; a PostgreSQL path is documented (see R-19) | VighneshHM | Mitigated |
| R-07 | Card cloning or credential theft: the mock reader presents only a UID, so a copied staff card would log in | security | Medium | High | Provider boundary carries a challenge-response `proof` field; lockout after repeated failures; every sign-in audited; deactivating a person ends sessions at once. A real challenge-response driver is not built | VighneshHM; Library IT for real cards | Open |
| R-08 | TLS misconfiguration: HTTP exposed on the LAN, weak or expired certificate, proxy mistakes | security | Medium | High | The app refuses half a TLS pair; its own HTTPS is tested with a real handshake; secure cookies and HSTS in production; binds to the LAN address only with TLS on. The reverse-proxy examples are documented, not run | Library IT | Open |
| R-09 | Supply chain: a vulnerable or tampered dependency or GitHub Action | security | Medium | High | Exact pins with hashes in `pyproject.toml`, `requirements.lock` and `requirements-dev.lock`; `pip-audit` fails the build (it forced `cryptography` 50.0.2, D-021); Dependabot weekly; SBOM and licence inventory per package | VighneshHM | Mitigated |
| R-10 | The Ed25519 verifier in `aisys_lifecycle/ed25519.py` is my own code, so a flaw could accept a forged package | security | Low | High | Tested against the RFC 8032 vectors and against a real library; installer pins its key; checksum per file, no unlisted file allowed. No external code review | VighneshHM | Accepted |
| R-11 | Backup theft: backups and the data file hold member names and contact details in clear text | security | Medium | High | Synthetic data only here. Backups stay on the server; access is by operating-system permissions. Encryption at rest and an off-machine copy are not built (`docs/remaining-work.md`) | Library IT | Open |
| R-12 | Insider misuse: a staff user reads or changes more than their job needs | security | Medium | Medium | Four roles with least privilege; every route declares its permission; 403s audited; append-only audit log by database trigger; card numbers masked in logs | VighneshHM | Mitigated |
| R-13 | Lock-out of the last administrator (deactivated, demoted or locked) | security | Low | High | The last active administrator cannot be deactivated or demoted, and nobody can deactivate or re-role themselves (409); a password lockout ends by itself or by Unlock; documented in `docs/administration.md` and `docs/troubleshooting.md`. A locked-out sole administrator has no tested server-side recovery command | VighneshHM | Mitigated |
| R-14 | Reminder or alert delivered twice or lost | technical | Medium | Medium | Queue first, deliver second; atomic claim of each message (D-029); unique index per loan, kind, channel and due date. A reminder missed while the application is down is not resent | VighneshHM | Mitigated |
| R-15 | Public catalogue or search is abused (scraping, wildcard queries) | security | Low | Low | 60 public searches a minute per address; wildcards escaped; page sizes bounded; the catalogue can be switched off | VighneshHM | Mitigated |
| R-16 | The SIP2 client has been tested only against a simulator written from the specification, not a real ILMS (DR-12 replaced the earlier exclusion, DR-05), so a vendor's field use, wording or timing could differ; NCIP 2.0 is not built | scope | High | High | Codec, session and adapter tested end to end over a real socket against the simulator, including lost, dropped and damaged answers; keyword mapping isolated in `adapter.py`; `aisys doctor` checks the connection; verify-before-resend prevents double loans; no conformance or certification claimed (A-11, `docs/sip2.md`). NCIP would be another class behind the same boundary. SIP2 has no encryption, so a tunnel is required | VighneshHM; Library IT for the real ILMS | Accepted |
| R-17 | No real hardware (readers, gate, camera, card printer) was used; drivers are mocks | scope | High | High | Interfaces proven by mocks with failure injection; site acceptance with real devices listed as remaining work | Library IT | Open |
| R-18 | UAT was run by the development team, not by library staff | scope | High | Medium | `docs/uat.md` states this; a site acceptance test is listed in `docs/remaining-work.md` | Library staff | Open |
| R-19 | SQLite allows one writer at a time; a busy site could outgrow it | technical | Low | Medium | WAL and busy timeout; measured figures in `docs/performance-results.md` (one process, one machine); PostgreSQL option listed as future work | VighneshHM | Accepted |
| R-20 | No penetration test and no content-security-policy (pages use inline scripts) | security | Medium | Medium | Static checks (Bandit rules in ruff), `docs/security-checks.md` lists the gap; headers, input validation and masking in place | VighneshHM | Accepted |
| R-21 | Linux and a real Windows Server 2022 installation are unverified (CI runs on the Server 2022 image only) | scope | Medium | Medium | Stated as unverified in `docs/deployment.md`; CI runs on `windows-latest` (Python 3.12, 3.13) and `windows-2022` | Library IT | Accepted |
| R-22 | CI slowness delays feedback: the test job reached 25 to 30 minutes (D-022, D-038) | schedule | Medium | Low | Test database is copied from a migrated template for each test; tests run in parallel; limit set to 30 minutes | VighneshHM | Mitigated |
| R-23 | Dependency: FastAPI (0.142.2) breaks or is vulnerable | dependency | Medium | Medium | Pinned exactly; `pip-audit` on every run; Dependabot; the test suite runs before any upgrade merges | VighneshHM | Mitigated |
| R-24 | Dependency: SQLAlchemy (2.1.3) behaviour change or vulnerability | dependency | Low | High | Pinned; audit; tests include migrations and database invariants | VighneshHM | Mitigated |
| R-25 | Dependency: Alembic (1.20.0) migration failure on upgrade | dependency | Low | High | Pinned; updates rehearsed on a copy of the data; backup before migrating; restore on failure | VighneshHM | Mitigated |
| R-26 | Dependency: `cryptography` (50.0.2) vulnerability (seven were found in 48.0.0, D-021) | dependency | Medium | Medium | Pinned at the fixed version; only in the `dev` extra and `requirements-dev.lock` (tests and signing), not shipped to the site; `pip-audit` | VighneshHM | Mitigated |
| R-27 | Dependency: openpyxl (3.1.5) or the spreadsheet parser mishandles a hostile file | dependency | Low | Medium | Pinned; imported rows are validated and staged before any record is created; CSV formulas neutralised on export | VighneshHM | Mitigated |
| R-28 | Dependency: uvicorn (0.54.0) or other runtime packages (Jinja2, python-multipart) vulnerable | dependency | Medium | Medium | Pinned; `pip-audit`; Dependabot; all 43 pinned packages in the licence inventory | VighneshHM | Mitigated |

## Dependencies

Python 3.12+, FastAPI, Uvicorn, SQLAlchemy, Alembic, Jinja2, python-multipart, openpyxl are pinned to exact versions in
`pyproject.toml`; every package including transitive ones is pinned with hashes in `requirements.lock` (runtime) or
`requirements-dev.lock` (development and build tools). Licences: `docs/third-party-licences.md`; redistribution and
offline-use notes: `docs/deployment.md`.

## Excluded work

Hardware, production systems and data, and certifications are outside the exercise (SOP section 3). Each one appears above as a
risk (R-05, R-16, R-17, R-18, R-20, R-21) so it is owned and visible, not just excluded.
