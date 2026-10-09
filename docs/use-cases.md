# Use cases (D1)

Actors: **Librarian** (catalogue, tagging, orders, serials, inventory, imports), **Desk clerk** (circulation only),
**Administrator** (people, cards, backups, updates), **Auditor** (read-only: audit log, reports, gate events), **Visitor**
(anyone using the public catalogue), **The ILMS** and the **devices** (staff reader, handheld, gate, camera, card printer,
mail/SMS/print services: all mocked here behind documented interfaces).

Each use case names its acceptance scenario (AC) or requirement, the page, and ends with its **acceptance criteria**: what
must be true for it to pass. Each criterion is checked by the UAT scenario named with it (`docs/uat.md`) and by the automated
tests tagged with the requirement (`docs/traceability.md` lists every test per requirement). All data in the examples is synthetic.

| ID | Use case | Actor | Req. | Page |
|---|---|---|---|---|
| UC-01 | Import a spreadsheet of holdings | Librarian | AC01, FR10 | Data migration |
| UC-02 | Add an item by hand, with net cataloguing | Librarian | AC02, FR01 | Catalogue |
| UC-03 | Search the catalogue (staff and public) | Librarian, Visitor | FR02 | Catalogue, `/opac` |
| UC-04 | Tag a book and see the tag-to-item link | Librarian | AC02, FR05 | RFID tagging |
| UC-05 | Take a tag off or retire it; re-tag | Librarian | FR05 | Tag registry |
| UC-06 | Check out, renew, check in | Desk clerk | AC03, FR04 | Circulation |
| UC-07 | Circulation is blocked | Desk clerk | AC04, FR04 | Circulation |
| UC-08 | Manage members, cards and fines | Librarian | FR04 | Circulation |
| UC-09 | Print a patron card | Librarian | FR04 | Circulation |
| UC-10 | Inventory a shelf with the handheld | Librarian | AC05, FR06 | Inventory |
| UC-11 | Manage shelves; see the virtual bookshelf | Librarian | FR06 | Shelves |
| UC-12 | Print spine and barcode labels | Librarian | FR01 | Labels |
| UC-13 | Order books and receive them | Librarian | FR01 | Acquisitions |
| UC-14 | Manage subscriptions and receive issues | Librarian | FR01 | Serials |
| UC-15 | An item leaves through the gate | Gate, Auditor | AC06, FR07 | Gate events |
| UC-16 | Staff are alerted by e-mail, SMS or print | Administrator | FR09 | Gate events |
| UC-17 | Patrons are reminded their loan is due | Desk clerk | FR09 | Circulation |
| UC-18 | Sign in with a staff card | Staff | AC07 | Sign-in |
| UC-19 | Look at the dashboard and reports | Librarian, Auditor | AC08, FR08 | Dashboard, Reports |
| UC-20 | Manage staff and cards; read the audit log | Administrator, Auditor | FR11 | Administration, Audit log |
| UC-21 | Back up, compare and restore | Administrator | AC10, NFR01 | Backups |
| UC-22 | Install, update and roll back offline | Administrator | AC09, FR12 | `manage.py`, Updates |
| UC-23 | Change the library's rules | Administrator | FR11 | Settings |
| UC-24 | Define a custom role | Administrator | FR11 | Administration |
| UC-25 | Circulation through the SIP2 protocol | Desk clerk | FR03, AC03 | Circulation |
| UC-26 | Correct a record; withdraw or write off a copy | Librarian | FR01 | Catalogue |
| UC-27 | Hand a screen over to visitors; a public terminal | Any staff, Visitor | NFR02, FR02 | Menu, `/opac` |

---

## UC-01 Import a spreadsheet of holdings
**Actor:** Librarian. **Goal:** bring about 20,000 existing records into the catalogue without touching what is there.
**Precondition:** signed in with `migration.run`; a `.csv` or `.xlsx` file.
**Main flow:** upload the file; the system stages every row verbatim, maps the columns (header aliases, adjustable); the
librarian reviews the validation report (invalid rows with the reason, duplicates and what they duplicate); commits; the system
takes a verified backup, inserts the valid rows in one transaction, and shows the reconciliation (source rows = migrated +
rejected + duplicates).
**Alternatives:** a row is invalid (kept in the error list, downloadable as CSV, never migrated); a row duplicates an existing
item (skipped and named); the commit fails (everything rolls back, the backup stays); the librarian rolls the batch back later
(only that batch's rows are removed).
**Result:** items and bibliographic records added; the original records are never updated or deleted.

**Acceptance criteria:**

- Every source row is staged; valid + invalid + duplicate = source rows in the reconciliation (AC01).
- Each invalid row is listed with its row number and reason, and the list downloads as CSV with formulas neutralised.
- A duplicate of an existing item is skipped and names what it duplicates; existing records are never updated or deleted.
- A verified backup exists before the commit; a failed commit changes nothing; rolling a batch back removes only its rows.
- A 20,000-row file validates and commits (measured 13.1 s and 7.4 s, `docs/performance-results.md`). UAT-01.

## UC-02 Add an item by hand, with net cataloguing
**Actor:** Librarian. **Flow:** **Add item**; optionally type an ISBN and **Look up the ISBN**, which fills title, author,
publisher, year, edition and language from the outside catalogue (a mock here); add the accession number, shelf and call number;
**Save**. The same validation as the import runs. **Alternatives:** unknown ISBN (warning), outside catalogue down (the error
says to enter it by hand), duplicate accession number or barcode (refused, naming the existing item).

**Acceptance criteria:**

- A valid ISBN fills title, author, publisher, year, edition and language; a bad check digit is refused before any lookup.
- The saved item passes the same validation as the import and is found by search at once (AC02). UAT-02.
- A duplicate accession number or barcode is refused and names the existing item.

## UC-03 Search the catalogue
**Actor:** Librarian, or a Visitor on `/opac`. **Flow:** type words; results appear while typing, best match first. Every word
must match; the last may be unfinished; accents and case are ignored. A fragment in the middle of a word still finds its book
when nothing matches by words. **Visitor view:** titles, shelves and availability only, with a **Show the shelf** picture;
never accession numbers, barcodes, tags, members or loans. **Limits:** 60 searches a minute per visitor.

**Acceptance criteria:**

- Every typed word must match (the last may be a prefix); accents and case are ignored; better matches come first.
- A new or edited item is searchable immediately (real-time indexing).
- The public page needs no sign-in and never shows accession numbers, barcodes, tags, members or loans; search text is shown as text, never as markup. UAT-03.

## UC-04 Tag a book
**Actor:** Librarian. **Flow:** open the item, **Tag this item**, read the reader, generate or read a blank tag, **Check** (is
the tag free, is the item free, is the item tagged already?), **Link tag to item**. The reader writes the accession number and
sets the security bit, then the database records the link; the page shows the item with its tag. **Alternatives:** the reader
fails (nothing is recorded), the database save fails (the reader write is undone), tag already on another item (refused and
named).

**Acceptance criteria:**

- The check reports every problem at once before anything is written (`docs/rfid.md` codes) (FR05, AC02).
- After **Link**, the tag shows on the item and the item on the tag, with the security bit armed. UAT-04.
- A reader failure saves nothing; a database failure after the write clears the tag again; a tag on another item is refused and that item named.

## UC-05 Remove, retire or re-tag
**Actor:** Librarian. **Flow:** in the tag registry choose a tag and **Remove** (it can be reused) or **Retire** (damaged or lost;
the number can never be linked again). A re-tag removes the old link and creates a new one, so the history of an item's tags is
kept.

**Acceptance criteria:**

- A removed tag can be linked again; a retired tag can never be; both leave a history line (FR05). UAT-05.
- Re-tagging needs explicit confirmation and keeps the old link in the item's history.

## UC-06 Check out, renew, check in
**Actor:** Desk clerk. **Precondition:** a member (by patron card, or found by name or number) and the item's tag on the reader
(or its number). **Flow:** **Check out**: the system checks the rules, asks the ILMS through the connector (with retries and the
same transaction id), then records the loan, the reader beeps and shows green. **Renew** and **Check in** likewise; a late
return charges a fine. **Result:** every action is one line in the append-only history.

**Acceptance criteria:**

- Check-out, renew and check-in each succeed once and each leaves one line in the append-only history (AC03). UAT-06.
- The same flow works through the mock ILMS and through SIP2 over TCP (UC-25); a timeout or lost answer never lends twice.
- A late return charges the fine the rules give; the due-date enquiry asks the ILMS.

## UC-07 Circulation is blocked
A reference item, a blocked member, a member whose fines exceed the limit, a member at the loan limit, an item already on loan,
an item lost or withdrawn: each is refused with the reason in plain words, the reader signals a warning, and the refusal is
recorded. A librarian with `circulation.override` may override where the policy allows.

**Acceptance criteria:**

- A reference item, a blocked member and a member whose fines exceed the configurable limit are each refused with the reason (AC04). UAT-07.
- The refusal is recorded in the history and the reader signals a warning; nothing is lent.

## UC-08 Manage members, cards and fines
**Actor:** Librarian. Add a member (optionally with e-mail and phone); issue or replace a card (the old one stops working at
once); block or unblock with a reason; record a payment or waive a fine. Fines are a ledger: the balance is the sum of its
entries.

**Acceptance criteria:**

- A new member gets the next number; contact details are validated (FR04). UAT-08.
- A card is linked only after the member record is validated (FR05, member side); the dry run lists every problem and warns that an old card will be switched off. UAT-26.
- A replacement card stops the old one identifying anyone at once; the balance always equals the ledger.

## UC-09 Print a patron card
**Actor:** Librarian. **Flow:** choose the member, **Print the card**: the card printer prints the name and number and the
chip is written with the card number; the page shows the card face (name, number, barcode) which the browser can also print.
**Alternatives:** no card yet (issue one first); printer jammed or offline (reported, recorded).

**Acceptance criteria:**

- The card printer job is created for the member's current card and the card face shows name, number and barcode (FR04). UAT-09.
- No card yet, or a jammed printer, gives a plain message and an audit line; nothing else changes.

## UC-10 Inventory a shelf
**Actor:** Librarian. **Flow:** choose the shelf, start a session, sweep with the handheld (as often as wanted; a tag read twice
counts once). Every tag earns a beep and a light; the report lists missing, misplaced, unknown and on-loan-but-on-the-shelf
books, and the books that are out of call-number order. Close the session to freeze the report; export CSV.

**Acceptance criteria:**

- A sweep lists found, missing, misplaced, unknown and on-loan-but-shelved tags; a tag read twice counts once (AC05). UAT-10.
- Every tag earns a beep and a light, recorded per tag; closing the session freezes the report; the CSV is safe to open.

## UC-11 Manage shelves; the virtual bookshelf
**Actor:** Librarian. **Flow:** the Shelves page lists every location with its count, loans and capacity; add or stop managing
a shelf (description, capacity); pick a shelf to see its books as spines in call-number order, coloured by status; books read
out of order at the last sweep are outlined.

**Acceptance criteria:**

- Shelves show count, loans and capacity; over capacity is marked; duplicates and bad input are refused (FR06). UAT-11.
- The virtual bookshelf is in call-number order and outlines the books found out of order at the last sweep.

## UC-12 Print labels
**Actor:** Librarian. **Flow:** choose a shelf or type item numbers; preview spine labels (call number across short lines) or
barcode labels (Code 128 of the barcode, or the accession number); print from the browser, or send the job to a label printer
(queued like any message).

**Acceptance criteria:**

- Spine labels are in shelf order and fit a narrow label; each barcode reads back as what was asked for (FR01). UAT-12.
- Text a barcode cannot hold falls back to the accession number; a print job is queued for the named label printer.

## UC-13 Order books and receive them
**Actor:** Librarian. **Flow:** create a draft order (supplier, lines with title, ISBN, copies, price, shelf); edit while a
draft; **Place**; when books arrive **Book in what arrived**: each copy is created in the catalogue with the next accession
number, all or nothing. **Alternatives:** receive more than ordered (refused), cancel (only before anything arrives).
Orders go draft, ordered, part received, received.

**Acceptance criteria:**

- An order moves draft, ordered, part received, received; receiving creates each copy with the next accession number, all or nothing (FR01). UAT-13.
- Receiving more than ordered is refused; only a draft can be edited; only an unreceived order can be cancelled.

## UC-14 Manage subscriptions
**Actor:** Librarian. **Flow:** add a subscription (title, ISSN, how often, first expected date); the schedule of expected issues
is created; **Received** on arrival (optionally adding the issue to the catalogue to tag and lend); issues more than a week late
appear under **Late issues** where they can be claimed from the supplier or given up.

**Acceptance criteria:**

- A wrong ISSN check digit is refused; the schedule follows the frequency without month-end drift (FR01). UAT-14.
- Issues more than a week late are listed and can be claimed or given up; a stopped subscription stops chasing.

## UC-15 An item leaves through the gate
**Actor:** the gate (mock), Auditor. **Flow:** the gate reads the tags passing; each tag's security bit is read at the gate (the
ILMS is not asked, so this works while it is down); an unissued or still-armed item raises an alarm event with its accession
number, a footfall count is kept, a camera snapshot is attached, and alerts are queued. A repeat alarm for the same tag within a
minute is recorded but not sent again.

**Acceptance criteria:**

- An unissued or still-armed item raises one alarm with its accession number, a camera snapshot and queued alerts (AC06). UAT-15.
- The decision uses only the security bit, so it works while the ILMS is down; footfall is counted; a camera failure still records and announces the alarm.
- In production a real gate is read by the gate loop, `aisys gate-poll` or **Read the gates now** (DR-18); an offline
  gate is named and the others are still read.

## UC-16 Staff are alerted
**Actor:** Administrator. Each staff member chooses e-mail, SMS and/or print and an address; alarms create queued messages in the
right words per channel (full e-mail, 160-character text, printed slip); delivery retries with back-off and can be retried by
hand; a mail outage never delays the alarm.

**Acceptance criteria:**

- Each staff member's chosen channels get a message in the right shape (e-mail, 160-character SMS, printed slip) (FR09). UAT-16.
- A failed delivery is retried with back-off and then sent; a provider outage never delays or loses the alarm event.

## UC-17 Patrons are reminded
**Actor:** the loop (and the Desk clerk who keeps contact details). **Flow:** a member with an e-mail or phone gets one message the
day before each loan is due and one on the due day. Once only per loan, kind, channel and due date; a returned or renewed loan's
queued reminder is cancelled; staff can switch reminders off per member.

**Acceptance criteria:**

- One message per loan, kind, channel and due date, the day before and on the due day (FR09). UAT-17.
- A returned or renewed loan's unsent reminder is cancelled; reminders can be switched off per member.

## UC-18 Sign in with a staff card
**Actor:** Staff. **Flow:** present the staff card at the reader (or password for the demo administrator); the system looks the
card up through the credential provider (a mock here) and opens a session with the person's role. **Rules:** a patron card never
signs anyone in; five wrong passwords lock the account for 15 minutes; a deactivated person or card is refused; every attempt is
audited with the card number masked.

**Acceptance criteria:**

- Each staff card signs in with its role's menu and rights; a patron card never signs in (AC07). UAT-18.
- Repeated wrong passwords lock the account; a deactivated person or card is refused; every attempt is audited with the card number masked.

## UC-19 Dashboard and reports
**Actor:** Librarian, Auditor. Dashboard tiles and charts; seven filterable reports (tagged items, members, circulation,
operators, RFID clients, fines, gate events) with CSV export that neutralises spreadsheet formulas.

**Acceptance criteria:**

- Seven reports with filters, summaries and CSV that matches the filters, not the page (AC08). UAT-19.
- Report and dashboard numbers match the data; bad filters are refused clearly; a CSV never runs formulas.

## UC-20 Manage staff; read the audit log
**Actor:** Administrator, Auditor. Add staff, issue staff or patron cards, activate or deactivate people and cards, unlock, change a
role (never your own; never leaving no administrator). The audit log filters by person, action, outcome and text, pages, and exports.

**Acceptance criteria:**

- Add staff, issue cards, deactivate and reactivate, unlock and change role, each audited (FR11). UAT-20.
- You cannot deactivate or demote yourself; the last administrator cannot be removed; the audit export follows the filter.

## UC-21 Back up, compare and restore
**Actor:** Administrator. **Flow:** **Back up now** (verified); **Compare** the live data with any backup, table by table;
**Restore** by typing RESTORE: the system makes a safety copy, pauses everyone else for a few seconds, copies the backup in,
re-checks every table against it and reverts if anything differs. Backups are also made before every import, restore and update.
**Proof (AC10):** after a simulated failed import or a damaging change, the comparison shows the library data identical to the backup.

**Acceptance criteria:**

- A backup is verified when made; compare reports table by table (AC10). UAT-21.
- After a simulated failed import or damaging change, a restore makes every library table identical to the backup; a restore that does not check out puts the previous data back.

## UC-22 Install, update and roll back offline
**Actor:** Administrator. **Flow:** install from a signed package (no internet); to update, stop the app and
`manage.py update <package>`: verify, back up, install beside the old version, rehearse on a copy, migrate, start once, switch.
**Rollback:** `manage.py rollback --yes` restores the previous version (and the pre-update data if the layout changed). A failed
step restores the data and keeps the old version.

**Acceptance criteria:**

- Install, update and rollback run with no internet; every connection off the machine is refused and logged, and the log stays empty (AC09). UAT-22.
- A tampered, foreign-signed or older package is refused before anything changes; after rollback the data is as before the update.

## UC-23 Change the library's rules
**Actor:** Administrator. **Flow:** open **Settings**, change a rule (loan days, renewals, fine rate and limit, reminder days,
gate duplicate window, sign-in limits, public catalogue switch), **Save**; or **Use file value** to go back to the configuration
file. Devices, drivers and secrets are shown read-only.

**Acceptance criteria:**

- A changed rule applies to the next transaction without a restart and survives a restart (FR11). UAT-23.
- Out-of-range, non-numeric, NaN or infinite values are refused in plain words; a batch with one bad value saves nothing.
- Only administrators see the page; every change and reset is audited with the old and new value.

## UC-24 Define a custom role
**Actor:** Administrator. **Flow:** on **Administration**, **Roles** panel: name the role, tick its permissions, **Add role**;
give it to a person in the staff list; **Edit** to change its permissions; **Remove** once nobody holds it.

**Acceptance criteria:**

- The person holding the role can do exactly what its permissions allow, from their next request (FR11). UAT-24.
- Built-in roles cannot be changed or removed; a held role cannot be removed; an administrator cannot take `admin.users` from
  their own role; bad names and unknown permissions are refused.
- Every create, change and removal is audited with the permissions added and removed.

## UC-25 Circulation through the SIP2 protocol
**Actor:** Desk clerk (the administrator configures it). **Flow:** with `[ilms] driver = "sip2"` and the ILMS (here the SIP2
simulator) reachable, the desk works exactly as in UC-06; `aisys doctor` reports the ILMS connection.

**Acceptance criteria:**

- Check-out, renew and check-in travel as SIP2 messages over TCP with checksums and are answered by the ILMS (FR03, AC03). UAT-25.
- A lost answer is verified with the ILMS before anything is sent again, so an item is never lent twice.
- The ILMS password comes only from the environment; the doctor line is OK when the ILMS answers and FAIL when it does not.

## UC-26 Correct a record; withdraw or write off a copy
**Actor:** Librarian. **Flow:** open the item in **Catalogue**, **Edit details or status**; correct the title, author, call number,
location or any other field, or set the status to damaged, lost, withdrawn or back to available with a reason; **Save changes**.
**Alternatives:** an invalid value (refused with the field and the reason, nothing saved); a barcode or title and ISBN that another
record has (refused, naming it); a copy on loan (its status stays until it is checked in).

**Acceptance criteria:**

- The merged record passes the same rules as a new item; the accession number cannot change (FR01). UAT-28.
- A correction is searchable at once; book details changed on one copy show on every copy of the title, and the audit line says
  how many copies that touched.
- A damaged, lost or withdrawn copy cannot be lent or tagged; a withdrawn copy leaves the public shelf; nothing is deleted.
- Every change is audited with the old and new values and the reason; a save that changes nothing writes nothing.

## UC-27 Hand a screen over to visitors; a public terminal
**Actor:** any member of staff; a Visitor. **Flow:** on a shared screen, **Sign out**, then choose **Public catalogue** on the
sign-in page. A public terminal opens the kiosk host name, where only the catalogue exists.
**Alternatives:** someone opens `/opac` by its address while signed in (nothing happens to the session; the page warns and offers
the sign-out); a link from another site to `/opac` (nobody is signed out).

**Acceptance criteria:**

- The staff menu has no public catalogue entry; the sign-in page links to it. The warning's sign-out ends the session on
  the server and in the browser, is audited with reason `public_catalogue`, and opens the catalogue; afterwards no staff
  page opens without signing in again (NFR02). UAT-27.
- Loading `/opac` never signs anyone out: not a link from another site, not a second tab, not an API client.
- A hand-over posted from another site is refused.
- On a kiosk host name only the catalogue, its search, static files and health checks answer; sign-in is impossible and any
  session sent is ignored; the staff host name is unaffected.
