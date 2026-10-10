# Reports and dashboard statistics (AC08, FR08)

A **Reports** page with seven filterable reports, each with summary figures, a chart, a paged table and a CSV download,
plus charts and an overdue list on the **Dashboard**. Everything is read-only: running or exporting a report changes
nothing (tested).

## The reports

| Report | Filters | Summary figures | Chart |
|---|---|---|---|
| **Tagged items** | dates, tag state, location, tagged by, search | tag links, in use, removed, retired, items with a tag | tag links by location |
| **Members** | status, owes / over the fine limit, overdue loans only, search | members, blocked, owing, total owed | largest balances |
| **Circulation** | dates, action, outcome, operator, member, item | transactions, done, refused, failed, members, items | transactions per day |
| **Operators** | dates, operator | staff, active in the period, actions, busiest | actions per person |
| **RFID clients** | dates, device kind | devices, online, offline, activity | activity per device |
| **Fines** | dates, kind, member, name | charged, paid, waived, opening balances, owed now | amount by kind |
| **Gate events** | dates, kind, gate, alarms only, search | events, alarms, unauthorised removals, repeats, footfall, messages sent / waiting | alarms per day |

"Operators" counts, per staff user: check-outs, renewals, check-ins, refused attempts, tags linked, inventory sessions,
payments taken and waivers given. "RFID clients" covers the staff reader (tag events), handheld (sessions and sweeps),
gate (passages, people, alarms) and camera (snapshots), with online state and last activity.

## Behaviour that matters

- **One set of filters for everything.** The summary figures, the chart, the table and the CSV all use the same filters,
  so what is at the top always adds up to what is listed (tested, for example paid + opening balances = owed now).
- **Dates are whole days in UTC.** "To" includes that whole day. A backwards range, an unreadable date or an unknown
  choice is refused with a clear message (422); filters a report does not have are ignored.
- **CSV matches the filters, not the page.** The export has every matching row (up to 50,000), money as decimals
  (`12.50`), and any cell starting with `=`, `+`, `-` or `@` is prefixed with `'` so a spreadsheet cannot run it as a
  formula (a title can start that way). Sent with `Cache-Control: no-store`.
- **Searching for only wildcards (`%`, `_`) matches nothing** rather than everything.
- **Quiet days are shown as zero** in per-day charts (the most recent 60 days at most).
- **The address bar remembers the report and filters**, so a page can be bookmarked, shared or reloaded.
- **Charts are inline SVG with no library**, each with an accessible description of its values and a data table beside it.

## Dashboard

For roles with `reports.view` (librarian, admin, auditor): completed transactions per day and gate alarms per day for the
last 14 days, and the five most overdue loans with the fine so far. The count tiles at the top show only what the
role may see: staff, cards, sessions and events for everyone; with `catalogue.read` the items, items with and without a tag,
on loan and overdue; with `member.read` the patrons (removed ones are not counted), how many are blocked, owe a fine or have
items out; with `gate.view` the gate alarms of the last 24 hours and the messages waiting to be sent.

## API (`reports.view`)

| Method | Path |
|---|---|
| GET | `/api/reports`: the catalogue (each report's filters and columns) |
| GET | `/api/reports/{id}?limit=&offset=&<filters>`: run a report |
| GET | `/api/reports/{id}/export.csv?<filters>`: the CSV (header `X-Total-Rows`) |
| GET | `/api/reports/charts?days=14`: the dashboard's series and overdue list |

Report ids: `tagged_items`, `members`, `circulation`, `operators`, `rfid_clients`, `fines`, `gate_events`.
Adding a report means writing one function and one entry in `reports/definitions.py`; the page, API and CSV pick it up.

## Known limits

- No scheduled or emailed reports, no saved filter sets, no PDF output.
- Charts are bar charts only. Totals are computed on request (fine for the exercise's size; very large catalogues would want summary tables).
- Dates are UTC days, not the library's local midnight.
- Patron-facing statistics (what a member borrowed) are not part of the staff reports.
