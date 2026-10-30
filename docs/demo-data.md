# The demo library (`seed-demo`)

`python -m shelfsense.cli seed-demo` fills an empty database with a small working library: about a hundred days of synthetic
activity, so every page of the staff menu has something to look at and test. Everything is invented: names, card numbers,
`.example` e-mail addresses and reserved `+1555…` test phone numbers.

| Command | What you get | Use it for |
|---|---|---|
| `seed-demo` | the full demo library described below (about 15 seconds) | trying the system, training, exploratory testing |
| `seed-demo --reset` | the full demo library, replacing the current database (a copy of the old one is kept) | starting again, or an old database that has only a few books |
| `seed-demo --minimal` | only the 4 staff and 6 patrons of the sign-in page, an empty catalogue | the scripted tours (`demo-script.md`, `uat.md`), which import and tag the sample file themselves |
| `install.py ... --seed-demo` | the full demo library, in an installed copy | a trial or training install, never live data |

The full library only goes into an **empty catalogue**: run on a database that already has books, `seed-demo` loads
nothing, only checks the staff and patron cards, says **NOT LOADED** and exits with an error. To replace the database with a
fresh demo library run `seed-demo --reset`: the old database file is kept beside it as `shelfsense.db.before-reset-<time>`.

## How it is built

Nothing is written straight into tables. `shelfsense.devtools.demo_seed` starts the application in its own process and replays
the history through the same HTTP API the pages use, signed in as the member of staff who would have done each step, so
the records, fines, audit lines and messages are exactly what the screens produce. History is replayed oldest first at the
moment it is meant to have happened (the application clock is moved for each step, DR-22), so a loan taken seven weeks ago
and returned late carries those dates and the fine those dates earn. The seeding run always uses the simulated ILMS,
devices and message providers, whatever the configuration file says: demo data never reaches a real ILMS, gate or mailbox.

## Staff (7)

The sign-in page still offers one card of each role; the others sign in with their card number typed in the card box.

| Name | Role | Card |
|---|---|---|
| Ada Admin | Administrator (also has the demo password `Demo-Admin-Pass-1`) | `STAFFADMIN01` |
| Leo Librarian | Librarian | `STAFFLIB0001` |
| Noor Haddad | Librarian | `STAFFLIB0002` |
| Cleo Clerk | Circulation clerk | `STAFFCLERK01` |
| Ravi Menon | Circulation clerk | `STAFFCLERK02` |
| Jonas Weber | Circulation clerk (his first card `STAFFCLERK3X` was reported lost and switched off) | `STAFFCLERK03` |
| Ari Auditor | Auditor | `STAFFAUDIT01` |

## Books (259)

* **191** from `library_200.xlsx`, imported on **Data migration** (7 invalid rows and 2 duplicates were refused), and **every
  one tagged**.
* **60** catalogued by hand on **Catalogue**; **15** of them are **left untagged** on purpose.
* **6** received from purchase orders (shelf *New Arrivals*, tagged on arrival), **2** booked in from serial issues (not
  tagged: 18 books in all have no tag).
* One copy each is **damaged**, **lost** and **withdrawn** (its tag taken off for reuse); one was moved to another shelf; one
  had a cracked tag replaced and retired. About 30 are on loan.

## Patrons (20)

| Patron | Situation to test |
|---|---|
| M0001 Priya Nair, M0002 Tom Becker, M0006 Kim Park | ordinary borrowers with past and current loans |
| M0003 Lena Okafor | **blocked** again for two overdue books (unblocked once before) |
| M0004 Sam Ortiz | owes 12.50, **over the fine limit**: a check-out attempt was refused |
| M0005 Ana Silva | owes 3.00, under the limit, still borrows |
| M0007 Meera Iyer | four books out (the limit is five) |
| M0008 Daniel Brooks | **replacement card** `PATRON000108` (the old card is switched off) |
| M0010 Omar Farouk | a late fine **waived** (book drop not emptied) |
| M0012 Lucas Moreau | asked for **no reminders** |
| M0014 Arjun Rao | owes a late fine: he returned a book a week late and has not paid yet |
| M0018 Oskar Lind | **removed** (moved away); found on **Patrons** with the *removed* filter |
| M0020 Yusuf Demir | joined this week; card issued two days later, nothing borrowed yet |

Fourteen patrons have **returned loans** going back about three months (some late, with the fine paid at the desk), and
fifteen have **current loans**: about a dozen are **overdue**, several were **renewed**, a few are due in the next days.
Patron cards are `PATRON0000nn` (`nn` = member number), except M0008 (above) and M0019 Chloe Bennett (`PATRON000019`).

## Every menu

| Page | What is in it |
|---|---|
| Dashboard | counts and the last day's activity drawn from everything below |
| Catalogue, RFID tagging, Tag registry | the books above; tags active, removed and retired with their history |
| Circulation | about 120 events: check-outs, check-ins, renewals, one refusal (fines) and one ILMS time-out handled cleanly |
| Patrons | the 20 patrons above |
| Inventory | sweeps of the Humanities and Science sections (closed: missing, misplaced, unknown tags) and the Reference Room (open) |
| Shelves | six shelves with descriptions and capacities |
| Labels | a barcode label run and a spine label run for *New Arrivals*; two patron cards printed on the card printer |
| Acquisitions | five orders: received, part-received, ordered, draft and cancelled |
| Serials | monthly, quarterly and weekly subscriptions with issues received (two booked into the catalogue), claimed, missing and late |
| Gate events | visitors' passages, borrowed books passing quietly, four alarms with snapshots (one without: the camera failed), alerts sent |
| Reports | every report has rows from the activity above |
| Data migration | the committed import, a donations file committed then rolled back, a corrections file checked but not imported |
| Audit log | about 750 entries by all seven staff, including a mistyped administrator password |
| Backups | the automatic backups taken before each import, and one made after the demo was loaded |
| Updates | only in a copy installed from a package (`install.py --seed-demo`): it shows that install |
| Settings | *Renewals allowed* changed to 3; a trial change of the fine per day, set back the next day |
| Administration | the seven staff and their cards, including the lost card that was switched off; alert recipients |

Reminders and gate alerts were sent through the simulated providers; this morning's text messages are waiting for a retry
because the simulated SMS gateway was failing, so the message list on **Gate events** shows sent messages and some
waiting for a retry.
