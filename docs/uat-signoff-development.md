# Acceptance sign-off: development-team copy (NFR05)

A filled copy of the master form `docs/uat-signoff.md`, for the acceptance run of the **development team** on the final build.
**It is not site acceptance.** The testers are the developer and the AI assistant, the devices are simulators, and the data is
synthetic. Site acceptance needs a fresh copy of `docs/uat-signoff.md` completed by library staff on the site's own system
(`docs/remaining-work.md`). Steps and expected results are in `docs/uat.md`; every scenario is also covered by automated tests
(`docs/traceability.md`), which pass on the same build.

The results below were recorded by the testers. **The decision (section 5) and the signatures (section 6) are left for the project
owner to complete.** No one has signed on anyone else's behalf.

## 1. Test details

| Field | Entry |
|---|---|
| Site / library name | none: development environment (demo library, synthetic data) |
| AISYS version (Updates page) | 0.1.0, branch `fix/sop-gaps` (the evaluation release candidate) |
| Environment (production-like, devices real or simulated) | Windows 11 developer PC; all devices simulated (mock drivers); ILMS: built-in mock, and the SIP2 simulator for UAT-25 |
| Database seeded with (demo data / site data) | demo data (`python -m aisys.cli seed-demo`), plus the synthetic sample spreadsheets in `samples/` |
| Date(s) of the test | 2026-10-05 to 2026-10-09 (each scenario when its feature was built); UAT-23 to UAT-26 on 2026-10-09 |
| Test lead (name, role) | VighneshHM, project owner and developer |

## 2. Testers

| Name | Role in the library | Role signed in as | Signature |
|---|---|---|---|
| VighneshHM | none (developer, project owner) | administrator, librarian, desk clerk, auditor (demo cards) | |
| Claude (AI assistant) | none (drove the in-app browser and read what each page showed) | as above | not applicable |

## 3. Scenarios

| ID | Scenario | Steps done | Result | Tester | Date | Comment |
|---|---|---|---|---|---|---|
| AC01 | Import, review, commit, reconcile, roll back (`uat.md` UAT-01) | ✓ | Pass | Development team | see `uat.md` | also the 20,000-row file: 13 s validate, 7 s commit, balanced |
| AC02 | Create or find an item, check, link a tag, relationship shown (UAT-02, UAT-04) | ✓ | Pass | Development team | see `uat.md` | |
| AC03 | Check out, renew, check in through the connector, history (UAT-06; over SIP2: UAT-25) | ✓ | Pass | Development team | UAT-25: 2026-10-09 | SIP2 against the simulator only, not a vendor ILMS |
| AC04 | Reference item, blocked member, member over the fine limit refused (UAT-07) | ✓ | Pass | Development team | see `uat.md` | |
| AC05 | Handheld shelf count, missing, misplaced, unknown, beeps and lights (UAT-10) | ✓ | Pass | Development team | see `uat.md` | handheld simulated |
| AC06 | Unissued item at the gate: alarm, accession number, picture, queued e-mail (UAT-15) | ✓ | Pass | Development team | see `uat.md` | gate and camera simulated |
| AC07 | Staff cards sign in by role; patron card refused; menus follow the role (UAT-18) | ✓ | Pass | Development team | see `uat.md` | credential provider simulated |
| AC08 | Dashboard and reports with filters; CSV safe (UAT-19) | ✓ | Pass | Development team | see `uat.md` | |
| AC09 | Install with no network, offline update, rollback (UAT-22) | ✓ | Pass | Development team | 2026-10-09 | `scripts/lifecycle_demo.py` with the network blocked; the log of attempted connections stayed empty |
| AC10 | Back up, damage or fail an import, restore, compare identical (UAT-21) | ✓ | Pass | Development team | see `uat.md` | |

Extra checks witnessed:

| ID | Scenario | Steps done | Result | Tester | Date | Comment |
|---|---|---|---|---|---|---|
| UAT-23 | Settings: a bad value refused; public catalogue switched off and back; audited | ✓ | Pass | Development team | 2026-10-09 | |
| UAT-24 | Custom role: create, assign, enforce, edit, refuse removal while held, remove; audited | ✓ | Pass | Development team | 2026-10-09 | D-047 and D-048 found and fixed during the run |
| UAT-26 | Patron card from the Administration page uses the member check; the old card stops | ✓ | Pass | Development team | 2026-10-09 | path fixed by D-050 |
| UAT-17 | Patron reminders | ✓ | Pass | Development team | see `uat.md` | |
| UAT-12 | Labels | ✓ | Pass | Development team | see `uat.md` | |
| UAT-03 | Public catalogue | ✓ | Pass | Development team | see `uat.md` | |
| UAT-09 | Card printing | ✓ | Pass | Development team | see `uat.md` | card printer simulated; D-034 found and fixed |

## 4. Defects raised

| No. | Scenario | What happened | Severity (high, medium, low) | Owner | Status (open, fixed, retested, accepted) |
|---|---|---|---|---|---|
| D-026, D-027 | UAT forms | field messages and layout (`defect-log.md`) | low | VighneshHM | fixed, retested |
| D-034 | UAT-09 | card preview stayed after another member was chosen | low | VighneshHM | fixed, retested |
| D-047 | UAT-24 | permission tick-boxes stretched across the panel | low | VighneshHM | fixed, retested |
| D-048 | UAT-24 | role holder counts stale until reload | medium | VighneshHM | fixed, retested |

No defect is open.

## 5. Overall decision

To be ticked by the project owner after reading sections 3 and 4.

- [ ] **Accepted as development evidence.** Every scenario passed and no defect is open; site acceptance still to follow.
- [ ] **Accepted with conditions.** Conditions: ______________________
- [ ] **Not accepted.** Scenarios to repeat: ______________________

## 6. Signatures

| Role | Name | Signature | Date |
|---|---|---|---|
| Project owner (accepts the development evidence) | VighneshHM | | |
| Library representative | not applicable to this copy: site acceptance uses `docs/uat-signoff.md` | | |
