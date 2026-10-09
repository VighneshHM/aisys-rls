# Project management and issue tracking (Stage 1, D4)

## Tracker

The tracker is the **GitHub repository `VighneshHM/aisys-rls`**: its pull requests, its Actions runs and (available for any
future item) its Issues.

To be plain about what was actually done: this was a single-developer, five-day exercise (5 to 9 October 2026), and work was
**tracked by pull requests and by the requirements table** (`docs/requirements.csv`, one row per FR, NFR and AC with a status),
not by a long list of GitHub Issues. Each pull request is the work item: its branch name says what it is for, its merge commit
records its number, and the requirement IDs it delivered are in the table below. Defects are not in GitHub Issues either; they
are in `docs/defect-log.md` (see "Defects" below). Nothing in this file was reconstructed from an issue board that did not exist.

Roles: **VighneshHM** is project owner, developer and the approver of every merge. The work was done with an AI assistant
(Claude) pair-programming in the owner's repository. The assistant proposes and writes code and documents; the owner reads,
decides, runs the checks and presses merge. The assistant does not review its own work as a second pair of eyes: review by
a person other than the author did not take place, and the owner's approval is the only human review.

## Delivered work, by pull request

Dates are the merge dates from `git log --merges`. Numbers #15 and #20 do not appear in the merge history of `main`, so they are not
listed and nothing is claimed about them.

| PR | Branch | Title (merge commit) | Merged | Requirements delivered |
|---|---|---|---|---|
| #1 | `feature/ac01-migration` | Import function for xml and csv | 2026-10-05 | AC01, FR10, NFR01 (import, staging, reconcile, rollback) |
| #2 | `ci/github-actions` | CI/CD pipeline | 2026-10-05 | NFR05, NFR06 (format, lint, tests, dependency scan) |
| #3 | `dependabot/github_actions/actions/setup-python-7` | Bump actions/setup-python from 5 to 7 | 2026-10-06 | NFR04 (dependency upkeep) |
| #4 | `dependabot/github_actions/actions/checkout-7` | Bump actions/checkout from 4 to 7 | 2026-10-06 | NFR04 (dependency upkeep) |
| #5 | `dependabot/pip/setuptools-gte-84.0.0` | Update setuptools requirement from >=68 to >=84.0.0 | 2026-10-06 | NFR04 (dependency upkeep) |
| #6 | `fix/stale-validation` | Import bugs fix | 2026-10-06 | AC01, FR10 (defect fixes) |
| #7 | `feature/ac02-tagging` | RFID tagging | 2026-10-06 | AC02, FR05, FR03 (device interfaces and mock reader) |
| #8 | `feature/ac03-circulation` | Circulation function and tagging UI/UX bug fix | 2026-10-06 | AC03, FR04, FR03 (connector boundary) |
| #9 | `feature/ac04` | Mark AC04 done | 2026-10-06 | AC04, FR04 (blocking rules) |
| #10 | `feature/ac05-inventory` | Inventory, fines and fixes | 2026-10-07 | AC05, FR06, FR04 (fines) |
| #11 | `feature/members` | Member management | 2026-10-07 | FR04 (members, patron cards) |
| #12 | `feature/ac06-gate` | Gate, notifications and member card display | 2026-10-07 | AC06, FR07, FR09 |
| #13 | `feature/ac08-reports` | Reports and dashboard charts | 2026-10-07 | AC08, FR08 |
| #14 | `feature/ac10-restore` | Backup and restore | 2026-10-08 | AC10, NFR01 |
| #16 | `feature/ac09-offline-lifecycle` | Offline install, update and rollback | 2026-10-08 | AC09, FR12, NFR04 (SBOM, licences) |
| #17 | `docs/final-walkthrough` | Walkthrough | 2026-10-08 | NFR08, NFR09 |
| #18 | `feature/audit-admin-pages` | Audit log, administration and production docs | 2026-10-08 | FR11, NFR02, NFR07 |
| #19 | `feature/patron-reminders` | Patron loan reminders and notification loop | 2026-10-08 | FR09 (reminders, A-25) |
| #21 | `overall-fixes` | Remaining requirements, search, shelves, acquisitions, serials, labels, deliverable documents, doc-generator ordering, parallel CI tests | 2026-10-09 | FR01, FR02, NFR03 to NFR09 (documents), NFR05 (CI speed-up) |
| #22 | `overall-fixes` | Settings page, type check, hashed lock file, SBOM, remaining deliverable documents, SIP2 wire protocol, bytecode fix for the signed package | 2026-10-09 | FR11 (Settings page), FR03 (SIP2), NFR04, NFR06, AC09 |
| (next) | `fix/sop-gaps` | Final SOP gap closure: custom roles, one member-card check, real network block in the offline rehearsal, split lock files, full licence inventory, stage timeline, UAT additions, release tag | open at the time of writing | FR03 (NCIP decision, DR-13), FR05, FR11, NFR04, NFR05, NFR09, AC09 |

Note on #21 and #22: both merged the same branch, `overall-fixes`. #21 (`3b11886`) carried `ad8a3b6`, `51c4a1b` and `f38be77`;
#22 (`2a1681c`) carried `39d38fe`, `d777380` and `6ef3588`. The last row is the branch that closes the gaps found by the final
coverage review; its pull-request number is given by GitHub when it is opened, so none is claimed here.
The requirement mapping above is by feature area and the notes column of `docs/requirements.csv`; the full test-level mapping
is the generated `docs/traceability.md`.

## Stage timeline, D0 to D10 (SOP section 9, NFR09)

SOP section 9 labels its stages with working days, D0 to D8, and NFR09 asks for the exercise to run from D0 to D10 (A-43: these are
days, not the deliverables D1 to D9 of section 7). The exercise ran over five calendar days, 5 to 9 October 2026, with the
deadline on 10 October. The days of the plan were therefore compressed, and the work was built in **vertical slices**, one
acceptance scenario per branch from design to test, rather than finishing every stage for every feature before starting the next.
That is a deviation from "follow the stages in order", stated here rather than hidden. Each stage's reviewable output still exists,
and the dates below come from `git log`.

| SOP day | Stage | Reviewable output | Where | Actual dates |
|---|---|---|---|---|
| D0 | 1 Initiation and controls | Repository and tracker; first requirements table; assumptions, questions, risks, dependencies, exclusions; synthetic data confirmed | `1ccefb4`; `docs/requirements.csv`; `docs/assumptions.md` (C-03 to C-06); `docs/risks.md` | 2026-10-05 |
| D1 to D2 | 2 Requirements and design | Use cases and acceptance criteria; domain model, schema, APIs, events, integration contracts, security, topology; mock contracts; design decisions | `docs/use-cases.md`; `docs/architecture.md`; `docs/decisions.md` DR-01 to DR-05 | 2026-10-05 to 2026-10-06 |
| D3 | 3 Foundation build | Project structure, configuration, authentication, roles, audit log, health checks, migrations, test framework, seed data, CI with static checks | `1ccefb4`, PR #2 | 2026-10-05 |
| D4 to D5 | 4 Core library functions | Cataloguing, members, circulation, fines, restrictions, search, reports, dashboards | PR #1, #6, #8, #9, #10, #11, #13, #21 | 2026-10-05 to 2026-10-09 |
| D6 | 5 RFID middleware and integrations | Device-neutral interfaces; ILMS boundary (SIP2 wire protocol added later); tagging, inventory, gate, notifications, smart card, camera; retries, timeouts, duplicates, offline behaviour | PR #7, #8, #10, #12, #19, #22 | 2026-10-06 to 2026-10-09 |
| D7 | 6 Migration and resilience | Profiling, validation, dry run, reconciliation, migration into the test database, rollback and restore | PR #1, #6, #14; `docs/samples/` | 2026-10-05 to 2026-10-08 |
| D8 | 7 Verification | Functional, integration, regression, performance, security, migration, backup, restore, offline-update and rollback tests; every result mapped to a requirement; defects triaged and retested | PR #16, #21, #22; `docs/traceability.md`, `docs/test-plan.md`, `docs/defect-log.md`, `docs/uat.md` | 2026-10-08 to 2026-10-09 |
| D9 | Demonstration (deliverable D9) | Demo script, presentation, known limitations, production backlog | `docs/demo-script.md`, `docs/presentation.html`, `docs/remaining-work.md` | 2026-10-09 |
| D10 | Hand-over | Final coverage review against the SOP, gap closure, evaluation release tag `v0.1.0` (the package version) | branch `fix/sop-gaps`; tag `v0.1.0` on `main` | 2026-10-09 to 2026-10-10 |

Work remaining for production readiness is listed in `docs/remaining-work.md`.

## Branching and commit rules

These are the rules actually followed (they are also in `docs/engineering.md`).

* `main` is always releasable. No direct commits to it; every change arrives by pull request.
* One short-lived branch per acceptance scenario or requirement, named `feature/<topic>`, `fix/<topic>`, `ci/<topic>`,
  `docs/<topic>`, or `overall-fixes` for the final cross-cutting pass. A branch starts from the current `main`.
* Commit messages are short and say what the commit adds or fixes ("addition of backup and restore", "fix doc generator ordering").
* The pipeline (format check, lint with the Bandit rules, tests on Windows, `pip-audit`) must pass before merge.
* A change that alters behaviour updates the requirement row, the design document, the code, the tests (marked with
  `@pytest.mark.req`) and the generated documents in the same pull request. `tests/test_docs.py` fails if a generated document is
  stale or a requirement marked done has no test.
* Dependency updates come from Dependabot as pull requests (#3, #4, #5) and go through the same gates.

## Definition of done

A requirement or scenario is `done` in `docs/requirements.csv` only when all of these hold:

1. The behaviour works end to end in the application, against the mock devices and connectors where the real ones are out of scope.
2. Automated tests exist and pass, including at least one that is tagged with the requirement ID.
3. The pipeline is green on the pull request (format, lint, tests, dependency audit).
4. The relevant document is written or updated, and generated documents (`data-dictionary.md`, `api-reference.md`, `traceability.md`)
   are regenerated.
5. Limits and exclusions are stated (in `docs/remaining-work.md` or the feature's document) rather than left implied.
6. Any defect found on the way is in `docs/defect-log.md` with its retest evidence.
7. The project owner has read the change and merged it. A requirement that is only partly met stays `partial` (FR03 was the one case until the SIP2 client was added on 2026-10-09; it is now `done` with stated limits, see `docs/sip2.md`).

## Defects

Defects are logged in `docs/defect-log.md`, one row each: ID (D-nnn), severity, area, how it was found, steps to reproduce, status,
owner, the pull request and version that fixed it, and the test or check that retested it. A defect is logged when it is found, fixed
in the branch that is current, and marked Fixed only once its retest evidence exists. A CI failure counts as a defect and is logged
the same way (D-021, D-022, D-037, D-038). Risks are tracked separately in `docs/risks.md` and questions to the project owner in the
clarification log in `docs/assumptions.md`.
