# Use cases (D1)

Actors: **Librarian** (catalogue, tagging, orders, serials, inventory, imports), **Desk clerk** (circulation only),
**Administrator** (people, cards, backups, updates), **Auditor** (read-only: audit log, reports, gate events), **Visitor**
(anyone using the public catalogue), **The ILMS** and the **devices** (staff reader, handheld, gate, camera, card printer,
mail/SMS/print services: all mocked here behind documented interfaces).

Each use case names its acceptance scenario (AC) or requirement, the page, and the tests that prove it
(`docs/traceability.md` lists every test per requirement). All data in the examples is synthetic.

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

## UC-02 Add an item by hand, with net cataloguing
**Actor:** Librarian. **Flow:** **Add item**; optionally type an ISBN and **Look up the ISBN**, which fills title, author,
publisher, year, edition and language from the outside catalogue (a mock here); add the accession number, shelf and call number;
**Save**. The same validation as the import runs. **Alternatives:** unknown ISBN (warning), outside catalogue down (the error
says to enter it by hand), duplicate accession number or barcode (refused, naming the existing item).

## UC-03 Search the catalogue
**Actor:** Librarian, or a Visitor on `/opac`. **Flow:** type words; results appear while typing, best match first. Every word
must match; the last may be unfinished; accents and case are ignored. A fragment in the middle of a word still finds its book
when nothing matches by words. **Visitor view:** titles, shelves and availability only, with a **Show the shelf** picture;
never accession numbers, barcodes, tags, members or loans. **Limits:** 60 searches a minute per visitor.

## UC-04 Tag a book
**Actor:** Librarian. **Flow:** open the item, **Tag this item**, read the reader, generate or read a blank tag, **Check** (is
the tag free, is the item free, is the item tagged already?), **Link tag to item**. The reader writes the accession number and
sets the security bit, then the database records the link; the page shows the item with its tag. **Alternatives:** the reader
fails (nothing is recorded), the database save fails (the reader write is undone), tag already on another item (refused and
named).

## UC-05 Remove, retire or re-tag
**Actor:** Librarian. **Flow:** in the tag registry choose a tag and **Remove** (it can be reused) or **Retire** (damaged or lost;
the number can never be linked again). A re-tag removes the old link and creates a new one, so the history of an item's tags is
kept.

## UC-06 Check out, renew, check in
**Actor:** Desk clerk. **Precondition:** a member (by patron card, or found by name or number) and the item's tag on the reader
(or its number). **Flow:** **Check out**: the system checks the rules, asks the ILMS through the connector (with retries and the
same transaction id), then records the loan, the reader beeps and shows green. **Renew** and **Check in** likewise; a late
return charges a fine. **Result:** every action is one line in the append-only history.

## UC-07 Circulation is blocked
A reference item, a blocked member, a member whose fines exceed the limit, a member at the loan limit, an item already on loan,
an item lost or withdrawn: each is refused with the reason in plain words, the reader signals a warning, and the refusal is
recorded. A librarian with `circulation.override` may override where the policy allows.

## UC-08 Manage members, cards and fines
**Actor:** Librarian. Add a member (optionally with e-mail and phone); issue or replace a card (the old one stops working at
once); block or unblock with a reason; record a payment or waive a fine. Fines are a ledger: the balance is the sum of its
entries.

## UC-09 Print a patron card
**Actor:** Librarian. **Flow:** choose the member, **Print the card**: the card printer prints the name and number and the
chip is written with the card number; the page shows the card face (name, number, barcode) which the browser can also print.
**Alternatives:** no card yet (issue one first); printer jammed or offline (reported, recorded).

## UC-10 Inventory a shelf
**Actor:** Librarian. **Flow:** choose the shelf, start a session, sweep with the handheld (as often as wanted; a tag read twice
counts once). Every tag earns a beep and a light; the report lists missing, misplaced, unknown and on-loan-but-on-the-shelf
books, and the books that are out of call-number order. Close the session to freeze the report; export CSV.

## UC-11 Manage shelves; the virtual bookshelf
**Actor:** Librarian. **Flow:** the Shelves page lists every location with its count, loans and capacity; add or stop managing
a shelf (description, capacity); pick a shelf to see its books as spines in call-number order, coloured by status; books read
out of order at the last sweep are outlined.

## UC-12 Print labels
**Actor:** Librarian. **Flow:** choose a shelf or type item numbers; preview spine labels (call number across short lines) or
barcode labels (Code 128 of the barcode, or the accession number); print from the browser, or send the job to a label printer
(queued like any message).

## UC-13 Order books and receive them
**Actor:** Librarian. **Flow:** create a draft order (supplier, lines with title, ISBN, copies, price, shelf); edit while a
draft; **Place**; when books arrive **Book in what arrived**: each copy is created in the catalogue with the next accession
number, all or nothing. **Alternatives:** receive more than ordered (refused), cancel (only before anything arrives).
Orders go draft, ordered, part received, received.

## UC-14 Manage subscriptions
**Actor:** Librarian. **Flow:** add a subscription (title, ISSN, how often, first expected date); the schedule of expected issues
is created; **Received** on arrival (optionally adding the issue to the catalogue to tag and lend); issues more than a week late
appear under **Late issues** where they can be claimed from the supplier or given up.

## UC-15 An item leaves through the gate
**Actor:** the gate (mock), Auditor. **Flow:** the gate reads the tags passing; each tag's security bit is read at the gate (the
ILMS is not asked, so this works while it is down); an unissued or still-armed item raises an alarm event with its accession
number, a footfall count is kept, a camera snapshot is attached, and alerts are queued. A repeat alarm for the same tag within a
minute is recorded but not sent again.

## UC-16 Staff are alerted
**Actor:** Administrator. Each staff member chooses e-mail, SMS and/or print and an address; alarms create queued messages in the
right words per channel (full e-mail, 160-character text, printed slip); delivery retries with back-off and can be retried by
hand; a mail outage never delays the alarm.

## UC-17 Patrons are reminded
**Actor:** the loop (and the Desk clerk who keeps contact details). **Flow:** a member with an e-mail or phone gets one message the
day before each loan is due and one on the due day. Once only per loan, kind, channel and due date; a returned or renewed loan's
queued reminder is cancelled; staff can switch reminders off per member.

## UC-18 Sign in with a staff card
**Actor:** Staff. **Flow:** present the staff card at the reader (or password for the demo administrator); the system looks the
card up through the credential provider (a mock here) and opens a session with the person's role. **Rules:** a patron card never
signs anyone in; five wrong passwords lock the account for 15 minutes; a deactivated person or card is refused; every attempt is
audited with the card number masked.

## UC-19 Dashboard and reports
**Actor:** Librarian, Auditor. Dashboard tiles and charts; seven filterable reports (tagged items, members, circulation,
operators, RFID clients, fines, gate events) with CSV export that neutralises spreadsheet formulas.

## UC-20 Manage staff; read the audit log
**Actor:** Administrator, Auditor. Add staff, issue staff or patron cards, activate or deactivate people and cards, unlock, change a
role (never your own; never leaving no administrator). The audit log filters by person, action, outcome and text, pages, and exports.

## UC-21 Back up, compare and restore
**Actor:** Administrator. **Flow:** **Back up now** (verified); **Compare** the live data with any backup, table by table;
**Restore** by typing RESTORE: the system makes a safety copy, pauses everyone else for a few seconds, copies the backup in,
re-checks every table against it and reverts if anything differs. Backups are also made before every import, restore and update.
**Proof (AC10):** after a simulated failed import or a damaging change, the comparison shows the library data identical to the backup.

## UC-22 Install, update and roll back offline
**Actor:** Administrator. **Flow:** install from a signed package (no internet); to update, stop the app and
`manage.py update <package>`: verify, back up, install beside the old version, rehearse on a copy, migrate, start once, switch.
**Rollback:** `manage.py rollback --yes` restores the previous version (and the pre-update data if the layout changed). A failed
step restores the data and keeps the old version.
