# Training plan (D8)

How the people who will use and look after the system learn it, using the system itself with synthetic data. Everything is
hands-on; nobody trains on real members.

## Audiences and what each needs

| Audience | Goal | Pages and documents |
|---|---|---|
| Desk clerks | Lend, renew and return; handle a refusal calmly; find a member | Circulation; `docs/user-guide.md`; `docs/troubleshooting.md` |
| Librarians | Catalogue, tag, inventory, shelves, labels, orders, serials, import, reports | Catalogue, RFID tagging, Tag registry, Inventory, Shelves, Labels, Acquisitions, Serials, Data migration, Reports; `docs/user-guide.md`, `docs/migration.md`, `docs/inventory.md`, `docs/rfid.md` |
| Security / gate staff | Read gate events, act on an alarm, keep the alert settings right | Gate events; `docs/gate.md` |
| Administrators | People and cards, backups, updates, health, configuration | Administration, Audit log, Backups, Updates; `docs/administration.md`, `docs/backup-restore.md`, `docs/deployment.md`, `docs/troubleshooting.md` |
| Auditors | Read the audit log and reports and trust them | Audit log, Reports; `docs/administration.md`, `docs/reports.md` |
| Visitors | Find a book | the public catalogue at `/opac` (no training: a one-page notice at the desk) |

## Course outline

| Module | For | Length | Content | Practice (with demo data) |
|---|---|---|---|---|
| 1. First steps | everyone | 30 min | What the system does; signing in with a staff card; the menu for your role; what a patron card is not | Sign in as each role; try the patron card and see why it is refused |
| 2. Circulation | clerks, librarians | 90 min | Find a member; check out, renew, check in; reading a refusal; fines; the reader's beeps and lights | Priya borrows two books; check out the blocked member (M0003) and the member over the limit (M0004); return a late book |
| 3. Members and cards | clerks, librarians | 45 min | Add a member with contact details; issue and replace a card; print a card; reminders and opt-out | Add a member, issue a card, print it, switch reminders off |
| 4. Catalogue and tagging | librarians | 90 min | Add an item (with ISBN lookup); search; tag, remove, retire, re-tag; reading the tag-to-item link | Add five books, tag three, retire one |
| 5. Inventory and shelves | librarians | 90 min | A shelf sweep; missing, misplaced, unknown, out of order; the virtual bookshelf; capacity | Sweep a shelf with stray and missing books; fix what the report lists |
| 6. Labels | librarians | 30 min | Spine and barcode labels; browser printing and the label printer | Print the labels for a shelf |
| 7. Orders and serials | librarians | 60 min | Make, place and receive an order; subscriptions, receiving issues, claiming late ones | Order and receive two titles; add a monthly journal and claim a late issue |
| 8. Importing | librarians | 60 min | The staged import: mapping, problems, duplicates, reconciliation, rollback | Import the 200-row sample, read the problem list, roll it back |
| 9. The gate | security staff, librarians | 45 min | Reading an alarm; the camera picture; alert preferences; what happens when mail is down | Walk a book through the gate; send and retry the queued message |
| 10. Reports | librarians, auditors | 45 min | Dashboard, the seven reports, filters, CSV | Answer three questions from the reports |
| 11. Administration | administrators | 90 min | People and roles, cards, locking out and unlocking, the audit log | Add a staff member, issue a card, deactivate, read your own actions in the audit log |
| 12. Keeping it safe | administrators | 90 min | Backups, comparing, restoring; the disaster drill; updating and rolling back offline; health checks and the doctor | Run the drill; install, update and roll back with `scripts/lifecycle_demo.py` |
| 13. When something goes wrong | administrators, librarians | 45 min | The troubleshooting table; where to look (health, audit log, status); who to call | Work through three faults from the table |

Suggested schedule for a new site: day 1 morning modules 1 to 3 (clerks), day 1 afternoon and day 2 modules 4 to 8 and 10
(librarians), day 2 afternoon modules 9, 11, 12 and 13 (administrators and security). Each module is a short talk, then the practice
on a training copy (`seed-demo`) that can be reset in a minute.

## Materials

`docs/user-guide.md` (a table of tasks), `docs/troubleshooting.md`, `docs/administration.md`, the feature documents, the
disaster drill and the demo script (`docs/demo-script.md`) as the trainer's walkthrough. Quick-reference cards to print: sign-in
and the four roles; the circulation refusals and what each means; the shelf-sweep colours and beeps; the update and rollback commands.

## Competence checks

A trainee is signed off when, without help, they can: (clerk) check out, renew and return, and explain a refusal; (librarian)
add and tag a book, run and close an inventory, import a file and read the reconciliation; (administrator) add and deactivate a
person, restore a backup after the drill, and apply and roll back an update on the training copy. The trainer ticks each task on a
one-page checklist and notes anything the screen made unclear (it feeds `docs/defect-log.md`).

## Go-live support

First week: an administrator on call, the troubleshooting table at the desk, a daily look at the audit log and the notification
queue. After a month: a refresher on modules 2 and 12. When the system is updated, the update notes (the package's `notes` field)
say what changed, and the Updates page keeps the history.
