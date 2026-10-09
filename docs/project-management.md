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
| #21 | `overall-fixes` | Remaining requirements, search, shelves, acquisitions, serials, labels, deliverable documents, performance fix, Settings page, CI speed-up | open at the time of writing (see note) | FR01, FR02, FR03 (documented limits), NFR03 to NFR09, FR11 (Settings page) |

Note on #21: the branch `overall-fixes` carries the commits of 2026-10-08 and 2026-10-09 listed in `git log` after PR #19
(`ad8a3b6`, `51c4a1b`, `f38be77`, plus uncommitted work in the working tree when this was written). The pull-request number 21 is
the number the project owner gave; there is no merge commit yet to confirm it, so it is not verified from git.
The requirement mapping above is by feature area and the notes column of `docs/requirements.csv`; the full test-level mapping
is the generated `docs/traceability.md`.

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
7. The project owner has read the change and merged it. A requirement that is only partly met stays `partial` (FR03 is the one case).

## Defects

Defects are logged in `docs/defect-log.md`, one row each: ID (D-nnn), severity, area, how it was found, steps to reproduce, status,
owner, the pull request and version that fixed it, and the test or check that retested it. A defect is logged when it is found, fixed
in the branch that is current, and marked Fixed only once its retest evidence exists. A CI failure counts as a defect and is logged
the same way (D-021, D-022, D-037, D-038). Risks are tracked separately in `docs/risks.md` and questions to the project owner in the
clarification log in `docs/assumptions.md`.
