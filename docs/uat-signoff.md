# Site acceptance test: sign-off form (NFR05)

**This form is blank.** It is for the site acceptance test that library staff will run on the installed system (the first
production task in `docs/remaining-work.md`). Nothing in it has been filled in or signed.

**The recorded UAT in `docs/uat.md` was run by the development team** (the developer, with the AI assistant driving the in-app
browser), **not by library staff**, and against simulated devices. It is development evidence, not acceptance. Only a completed and signed copy of this
form, made by library staff on the site's system, is acceptance evidence.

Copy this file for each run; fill in a copy, never this master. Use the steps in `docs/uat.md` and `docs/demo-script.md` for each
scenario.

## 1. Test details

| Field | Entry |
|---|---|
| Site / library name | |
| AISYS version (Updates page) | |
| Environment (production-like, devices real or simulated) | |
| Database seeded with (demo data / site data) | |
| Date(s) of the test | |
| Test lead (name, role) | |

## 2. Testers

| Name | Role in the library | Role signed in as | Signature |
|---|---|---|---|
| | | | |
| | | | |
| | | | |

## 3. Scenarios

Mark **steps done** with a tick, **result** with Pass, Fail or Not run, and write the tester's name and the date. A Fail needs a
defect number in section 4.

| ID | Scenario (what the tester does) | Steps done | Result | Tester | Date | Comment |
|---|---|---|---|---|---|---|
| AC01 | Import a spreadsheet: stage it, review problems and duplicates, commit, read the reconciliation, roll back (`uat.md` UAT-01) | | | | | |
| AC02 | Create or find an item, check it, link a tag, see the tag-to-item relationship (UAT-02, UAT-04) | | | | | |
| AC03 | Check out, renew and check in through the ILMS connector; the history shows each step (UAT-06) | | | | | |
| AC04 | Circulation is refused for a reference item, a blocked member and a member over the fine limit, with the reason (UAT-07) | | | | | |
| AC05 | A handheld counts a shelf; missing, misplaced and unknown items are listed; beeps and lights confirm (UAT-10) | | | | | |
| AC06 | An unissued item passes the gate; an alarm with the accession number, a camera picture and a queued e-mail appear (UAT-15) | | | | | |
| AC07 | Sign in with each staff card; a patron card is refused; each role sees only its own menu (UAT-18) | | | | | |
| AC08 | The dashboard and each report show correct numbers with filters; the CSV opens without running formulas (UAT-19) | | | | | |
| AC09 | Install and run with no network, apply an offline update, roll back (UAT-22; command line, with the site's administrator) | | | | | |
| AC10 | Back up, damage or fail an import, restore; compare shows every table identical (UAT-21) | | | | | |

Optional extra checks the site wants to witness (add rows): Settings page change and revert, patron reminders, labels, public
catalogue, card printing.

| ID | Scenario | Steps done | Result | Tester | Date | Comment |
|---|---|---|---|---|---|---|
| | | | | | | |
| | | | | | | |

## 4. Defects raised

| No. | Scenario | What happened | Severity (high, medium, low) | Owner | Status (open, fixed, retested, accepted) |
|---|---|---|---|---|---|
| | | | | | |
| | | | | | |
| | | | | | |

## 5. Overall decision

Tick one.

- [ ] **Accepted.** Every scenario passed and no defect is open.
- [ ] **Accepted with conditions.** Listed defects must be fixed by (date): ______________________
- [ ] **Not accepted.** Scenarios to repeat after the fixes: ______________________

Reasons and conditions:

&nbsp;

&nbsp;

## 6. Signatures

| Role | Name | Signature | Date |
|---|---|---|---|
| Library representative (accepts for the library) | | | |
| Test lead | | | |
| Administrator of the system | | | |
| Supplier representative | | | |

The completed form is kept with the backups and the audit-log export for the test period (`docs/administration.md`).
