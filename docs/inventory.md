# Inventory (AC05, FR06)

Staff walk a shelf with a handheld reader. The system reports what is found, missing, misplaced or unknown, and every
tag earns a confirmation (a beep and a light) that is stored.

## How it works

1. **Start a session** for a shelf location (items already carry a `location`). One session = one location and one handheld.
2. **Sweep** as often as needed. Each sweep reads every tag in range. The same tag usually comes back several times,
   so tags are de-duplicated per session and only a read counter grows.
3. **Report** (live while open, frozen when closed), with CSV export.
4. **Close** the session. Its counts are stored and no longer change, even if the catalogue changes afterwards.

| Result | Meaning | Beep and light |
|---|---|---|
| **found** | the item belongs here and is where it should be | `ok`: 1 beep, green |
| **missing** | belongs here, is available, has a tag, and was **not** read | (no read, so no signal) |
| **misplaced** | a known item that belongs in another location | `warning`: 2 beeps, amber |
| **shelved on loan** | belongs here, recorded as on loan, yet on the shelf (returned but not checked in) | `warning` |
| **unknown** | not an active tag of any item: never registered, removed, or retired | `error`: long beep, red |

Items **on loan** are not expected on the shelf, and **untagged** items cannot be read by RFID, so both appear as
counts and are never reported as missing.

## Design decisions

| Decision | Why | Cost |
|---|---|---|
| A `Handheld` device interface (`scan`, `indicate`) next to the staff reader, built from `[[devices]]` config with `kind = "handheld"` | Same pluggability as the staff reader; real hardware is a registered driver | The mock is the only driver |
| De-duplicate per session, count reads | Real readers repeat tags; the report must not double count (tested) | No per-read timeline, only first and last seen |
| Signals are decided by the server and stored per tag; the device is told to beep but a broken buzzer never loses a scan | "Visible and audible confirmation events" must be evidence, not only a sound | Stored per distinct tag, not per read |
| Missing is computed at report time from the live catalogue; closing freezes the whole report (counts and lists) | An open session reflects reality; a closed one is a record that later catalogue changes cannot rewrite | The closed report is stored as JSON in the session row (fine at this scale) |
| CSV cells starting with `=`, `+`, `-` or `@` are prefixed with `'` | Stops spreadsheet formula injection from a title (tested) | A leading `'` shows in the cell |

## API (`/api/inventory`, permission `inventory.run`)

| Method | Path |
|---|---|
| GET | `/locations`: shelf locations with item, tagged and on-loan counts |
| GET | `/handhelds` |
| GET / POST | `/sessions` (list / start `{location, handheld_id}`) |
| GET | `/sessions/{id}` the report; `/sessions/{id}/export.csv` |
| POST | `/sessions/{id}/scan` one sweep; `/sessions/{id}/close` |
| simulator | `/mock/handhelds/{id}/load-shelf` builds a shelf from the catalogue (missing, strays, unknown tags, duplicates); `/mock/handhelds/{id}/power` (404 in production) |

Errors: 404 unknown location / handheld / session, 409 session already closed, 503 handheld offline, 502 handheld error.

## Known limits

- Only a mock handheld. No real driver, no antenna-level or aisle-level positioning.
- The report does not move books: correcting a misplaced item is a manual step (or a catalogue edit).
- The inventory is by location text; there is no shelf-level hierarchy and no scheduled inventory plan.
