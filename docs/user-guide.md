# User guide for library staff (NFR08)

Sign in by presenting your **staff card** at the reader (in the demo, use the simulated card list on the sign-in page). A patron
card never signs anyone in. Your menu shows only what your role may do.

| I want to... | Go to | Steps |
|---|---|---|
| Check an item out | **Circulation** | Present the patron card (or search the member), place the item's tag on the reader, **Check out**. If it is refused, the message says why. |
| Return or renew | **Circulation** | Check in or Renew the item. A late return adds a fine to the member. |
| The library system is not answering | **Circulation** | If a check-out, renew or check-in says to try again, the library system (ILMS) did not answer. Nothing was recorded. Wait a moment and try again; if it keeps happening, tell the administrator (`docs/troubleshooting.md`). |
| Set where a member's reminders go | **Circulation** | Choose the member, **Contact and reminders**: e-mail, phone, on or off. They are told a day before a loan is due and on the due day. |
| See every member, or remove one who has left | **Patrons** | Search or use the buttons (*Blocked*, *Items out*, *Owe a fine*, *Removed*). **Remove** keeps their history and stops them borrowing; it is refused while items are out or a fine is owed. **Restore** undoes it. Administrators can also **Erase details** for good. |
| Find a book (for a visitor) | `/opac` (no sign-in) | Type words; results appear as you type; **Show the shelf** shows where it stands. **Return to home page** goes back to the sign-in page |
| Leave this screen for visitors | **Sign out**, then **Public catalogue** on the sign-in page | The catalogue is not in the staff menu. If you open `/opac` by its address while signed in, you are **not** signed out: the page tells you and offers **Sign out and keep the catalogue open**. Public terminals use their own address, where staff pages do not exist |
| Look at a shelf | **Shelves** | Choose a shelf: its books in call-number order, coloured by status; red outline = out of order after the last sweep |
| Print labels | **Labels** | Choose a shelf or type item numbers, preview, print or send to the label printer; **Recent print jobs** shows what was sent and whether it printed |
| Order books | **Acquisitions** | New order, add lines, **Place order**; when books arrive **Book in what arrived** |
| Track a journal | **Serials** | Add the subscription; **Received** when an issue arrives; claim the late ones |
| Print a member's card | **Circulation** | Choose the member, **Print the card** |
| Correct a record, or mark a copy damaged, lost or withdrawn | **Catalogue** | Open the item, **Edit details or status**, change what is wrong, give a reason for a status change, **Save changes**. The accession number cannot change; book details are shared by every copy of the title; nothing is deleted (a copy that leaves is withdrawn). A copy on loan keeps its status until it is checked in |
| Tag a new book | **Catalogue**, then **RFID tagging** | Open the item, **Tag this item**, read the reader, generate or read a tag, **Check**, **Link tag to item**. |
| Take a tag off a book | **Tag registry** | Choose the tag, then remove it (it can be reused) or retire it (never again). |
| Count a shelf | **Inventory** | Start a session for a location, run the handheld sweep, close it: found, missing, unexpected. Export CSV. |
| See gate alarms | **Gate events** | Filter by kind or alarms only; open an event for its camera picture; **Send queued messages** to deliver alerts. The gates are read by themselves every few seconds; **Read the gates now** reads them at once. |
| Import a spreadsheet | **Data migration** | Upload, map columns, validate, review problems, commit; roll back if needed. |
| Look at the numbers | **Reports** and **Dashboard** | Pick a report, set filters, export CSV. |
| Back up or restore | **Backups** (administrators) | Back up now; compare with the live data; restore by typing RESTORE. |
| Check versions and updates | **Updates** (administrators) | See the installed version, licence and history; check a waiting package. |
| Manage staff and cards | **Administration** (administrators) | Add people, issue cards, deactivate, unlock, change role. |
| Make a role for a special job | **Administration**, **Roles** (administrators) | Add a custom role, tick its permissions, give it to people; edit or remove it later (not while someone holds it). The four built-in roles cannot be changed |
| Change the library's rules | **Settings** (administrators) | Loan period, renewals, fines, reminders, sign-in limits, public catalogue on or off; **Save**, or **Use file value** to undo. Changes apply at once and are audited |
| Check the library system connection | **Settings**, *Library system (ILMS)* | Shows the driver (mock or SIP2) and address; `python -m aisys.cli doctor` tests it (`docs/sip2.md`) |
| See who did what | **Audit log** | Filter by person, action and outcome; export CSV. |

If something does not work, see `docs/troubleshooting.md`.
