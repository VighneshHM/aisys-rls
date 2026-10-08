# User acceptance test script and results (D6, NFR05)

One scenario per use case in `docs/use-cases.md`. Each has the steps a library person would follow, what they should see, and the
recorded outcome. Setup for all: a fresh database (`python -m aisys.cli seed-demo`) and `python -m aisys.cli run`, then
http://127.0.0.1:8000. Demo identities (all synthetic): staff cards `STAFFADMIN01` (administrator), `STAFFLIB0001` (librarian),
`STAFFCLERK01` (desk clerk), `STAFFAUDIT01` (auditor); demo patron `PATRON000001` (Priya Nair, M0001).

**Who ran them:** the development team (the developer, with the AI assistant driving the in-app browser and reading what the
page showed), not library staff. That is the stated limit of this evidence: a site acceptance test with real users and devices is
the first production task (`docs/remaining-work.md`). Each scenario was run in the browser when its feature was built, and the
whole set of automated checks that mirror them (`docs/traceability.md`) passes on the final build.

| ID | Use case | Steps | Expected | Outcome |
|---|---|---|---|---|
| UAT-01 | UC-01 import | Sign in as the librarian. **Data migration**, upload `samples/library_200.xlsx`; review problems and duplicates; **Commit**; open the reconciliation; **Roll back** the batch | Counts add up (valid + invalid + duplicate = rows); the error list can be downloaded; after rollback the catalogue is as before | **Pass** (also the 20,000-row file: 13 s validate, 7 s commit, balanced) |
| UAT-02 | UC-02 add an item, net cataloguing | **Catalogue**, **Add item**, type ISBN `9780001000032`, **Look up the ISBN**, add accession `UAT-1`, shelf `Main/A1`, **Save** | The form fills with "A Short History of Quiet"; the item is saved and found by search; a bad or unknown ISBN gives a clear message | **Pass** (browser, final build) |
| UAT-03 | UC-03 search | Type `dispos` in the catalogue; then open `/opac` signed out and search `le guin`, press **Show the shelf** | Results appear while typing, best first; the public page shows titles, shelves and availability only; the shelf appears as a row of spines with the book outlined | **Pass** (browser, final build; XSS text `<img onerror>` shown as text) |
| UAT-04 | UC-04 tag a book | **RFID tagging**: choose the item, read the reader, generate a tag, **Check**, **Link tag to item** | The tag is shown on the item with the security bit armed; a tag already used is refused with the item named | **Pass** |
| UAT-05 | UC-05 remove, retire | **Tag registry**: remove one tag, retire another, try to link the retired number again | Removed tag can be reused; retired cannot; history lists both | **Pass** |
| UAT-06 | UC-06 circulation | As the desk clerk: choose Priya, put an item's tag on the reader, **Check out**, **Renew**, **Check in** | Each step succeeds with a beep and the history shows three lines; a late return adds a fine | **Pass** |
| UAT-07 | UC-07 blocks | Try a reference item, the blocked member (M0003), the member over the fine limit (M0004) | Each is refused with its reason; the reader shows the warning; the refusals are in the history | **Pass** |
| UAT-08 | UC-08 members, cards, fines | Add a member with e-mail and phone; issue a card; replace it; block and unblock; record a payment | The old card stops identifying; the balance equals the ledger | **Pass** |
| UAT-09 | UC-09 print a card | Choose Priya, **Print the card**; then choose a member with no card | The card printer job number is shown and the card face appears with a barcode; the preview disappears when another member is chosen; a member without a card gets "issue a card first" | **Pass** (browser, final build; a defect found and fixed: D-034) |
| UAT-10 | UC-10 inventory | **Inventory**: choose a shelf, simulate a handheld with missing and stray books, scan, close | Missing, misplaced and unknown lists with beeps and lights; closing freezes the report; out-of-order books are listed | **Pass** |
| UAT-11 | UC-11 shelves | **Shelves**: add shelf `Main/A1` with capacity 4; open it | The shelf lists 6 books against capacity 4 (over capacity, red bar); spines in call-number order; the three books out of order after the sweep are outlined; duplicates are refused | **Pass** (browser, final build) |
| UAT-12 | UC-12 labels | **Labels**: shelf `Main/B2`, spine preview, barcode preview, send to `label-printer`; then a bad printer name | 4 spine labels in shelf order; barcode labels with bars; "4 label(s) queued"; the bad name is refused with advice | **Pass** (browser, final build) |
| UAT-13 | UC-13 acquisitions | **Acquisitions**: new order for `The Lighthouse Ledger` (2 copies) with ISBN `9780001000018`; add a line; place; book in 1 of the first and the second; try to book in too many | Draft, ordered, part received; two items `ACQ000001` and `ACQ000002` in the catalogue; "only 1 still to come" for too many | **Pass** (browser, final build) |
| UAT-14 | UC-14 serials | **Serials**: add `Journal of Quiet Things` weekly starting 40 days ago with a wrong ISSN, then the right one; claim the first late issue; receive the second with "add to the catalogue" | Wrong ISSN explained; 5 late issues; one claimed; one received and added; the late count drops | **Pass** (browser, final build) |
| UAT-15 | UC-15 gate | **Gate events**: walk an unissued book through the simulated gate | An alarm event with the accession number, a camera snapshot and a queued e-mail; footfall counted | **Pass** |
| UAT-16 | UC-16 alerts | Set staff alert preferences; **Send queued messages**; make the e-mail provider fail once | Messages in the right shape per channel; the failed one retries and then sends | **Pass** |
| UAT-17 | UC-17 reminders | Give Priya an e-mail and phone; on **Circulation** use **Due tomorrow** and **Due today** beside two loans; wait for the loop (3 s) | Four messages (e-mail and SMS for each) are sent once and appear under *Reminders sent*; nothing more on later passes; returning the item cancels an unsent reminder | **Pass** (live run with the loop on) |
| UAT-18 | UC-18 card sign-in | Sign in with each staff card; try the patron card; try five wrong passwords | Roles show the right menu; the patron card is refused; the account locks and an administrator can unlock it | **Pass** |
| UAT-19 | UC-19 dashboard, reports | Open the dashboard; run each report with filters; export CSV | Numbers match the data; the CSV opens without running formulas | **Pass** |
| UAT-20 | UC-20 administration, audit | As the administrator: add a staff member and a card; deactivate and reactivate; try to deactivate yourself. As the auditor: filter the audit log, export | Self-protection refuses; the audit log shows each change with masked card numbers; the export follows the filter | **Pass** (browser, final build) |
| UAT-21 | UC-21 backup and restore | **Backups**: back up; run the disaster drill (damaging change, then restore); arm a failing import and commit one; compare | Damage shown table by table, then every table identical after restore; the failed import leaves the library data identical to the backup | **Pass** |
| UAT-22 | UC-22 offline lifecycle | `python scripts/lifecycle_demo.py` (network disabled): install 0.1.0, refuse a tampered, a foreign-signed and an older package, update, roll back; open **Updates** | Each refusal explains itself; after the update the new table exists with all data; after the rollback the data is as before; the Updates page shows versions, licence and history, and dry-run checks a waiting package | **Pass** |

## Layout, theme and keyboard checks (usability)

| Check | Result |
|---|---|
| Every page at desktop width and at phone width (375 px): no horizontal page scroll, navigation reachable | Pass (Updates, Administration and Audit pages measured; other pages share the layout) |
| Dark and light theme: text contrast of the new pages (for example command blocks 17:1 in light) | Pass |
| Every action button disables while working, so a double click does one thing (restore, add staff, issue card, ISBN lookup) | Pass |
| Empty states and errors say what to do ("No members yet: run seed-demo", "Enter the details by hand") | Pass |
| Forms show the specific field and reason, not "check the highlighted fields" | Pass after D-026 |
| A long list scrolls in its own panel or pages (staff 150+, backups 800+, audit 170+, shelf 300 at a time) | Pass |

## Defects found by this run

D-026, D-027, D-034 (`docs/defect-log.md`), all fixed and retested. No scenario fails and no defect is open.
