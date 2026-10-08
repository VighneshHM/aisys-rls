# User guide for library staff (NFR08)

Sign in by presenting your **staff card** at the reader (in the demo, use the simulated card list on the sign-in page). A patron
card never signs anyone in. Your menu shows only what your role may do.

| I want to... | Go to | Steps |
|---|---|---|
| Check an item out | **Circulation** | Present the patron card (or search the member), place the item's tag on the reader, **Check out**. If it is refused, the message says why. |
| Return or renew | **Circulation** | Check in or Renew the item. A late return adds a fine to the member. |
| Tag a new book | **Catalogue**, then **RFID tagging** | Open the item, **Tag this item**, read the reader, generate or read a tag, **Check**, **Link tag to item**. |
| Take a tag off a book | **Tag registry** | Choose the tag, then remove it (it can be reused) or retire it (never again). |
| Count a shelf | **Inventory** | Start a session for a location, run the handheld sweep, close it: found, missing, unexpected. Export CSV. |
| See gate alarms | **Gate events** | Filter by kind or alarms only; open an event for its camera picture; **Send queued messages** to deliver alerts. |
| Import a spreadsheet | **Data migration** | Upload, map columns, validate, review problems, commit; roll back if needed. |
| Look at the numbers | **Reports** and **Dashboard** | Pick a report, set filters, export CSV. |
| Back up or restore | **Backups** (administrators) | Back up now; compare with the live data; restore by typing RESTORE. |
| Check versions and updates | **Updates** (administrators) | See the installed version, licence and history; check a waiting package. |
| Manage staff and cards | **Administration** (administrators) | Add people, issue cards, deactivate, unlock, change role. |
| See who did what | **Audit log** | Filter by person, action and outcome; export CSV. |

If something does not work, see `docs/troubleshooting.md`.
