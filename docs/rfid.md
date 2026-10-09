# RFID layer and tagging (AC02, FR03, FR05) - design, rules and evidence

## What it does
A librarian creates or finds an item (a physical copy), puts a tag on the staff reader, the system **validates** the pairing,
the reader **writes the accession number and arms the security bit**, and the tag-to-item relationship is saved and shown
(from the tag side and from the item side), with a history and a monitoring log.

```
Staff reader (mock)  <-- encode / set_security / clear / indicate --   Tagging service   --> SQLite
        |  read_field -> TagObserved events  -->                        validate -> device -> DB
        v                                                               (compensate on failure)
 DeviceRegistry (built from [[devices]] in config.toml)                                   rfid_tags, tag_events, audit_events
```

## Layers (SOP section 6)
| Layer | Where | Status |
|---|---|---|
| RFID middleware: device-neutral commands/events | `src/aisys/rfid/devices.py`, `events.py` | Staff reader done; handheld, gate, camera, printer pending |
| Device adapters | `rfid/mock.py` (simulator), `rfid/registry.py` | Mock done; real vendor driver = one class + `register_driver` |
| Application services | `rfid/tagging.py`, `catalogue/service.py` | Done for tagging |
| API | `rfid_routes.py`, `catalogue_routes.py` | Done |
| UI | `/catalogue`, `/tagging` | Done |

## Technical decisions (and why)
| Decision | Why | Trade-off |
|---|---|---|
| **Protocol interfaces** for devices; the app never imports a vendor SDK | The SOP's device boundary; a real reader needs one class; tests use the same contract | A little indirection |
| **Devices come from `[[devices]]` config** (id, kind, driver); bad config fails at startup | NFR06 (config outside code); a typo is caught at boot, not at the first scan | Drivers are registered in code, not discovered as plugins |
| **Tag binds to the item (copy)**, not the title | Each physical copy has its own tag; circulation and the gate work per copy | Member-card tagging is separate (needs member records, AC03/04) |
| **One active tag per item** enforced by a *partial unique index* in the database | Cannot be broken even by code that bypasses the service (tested) | SQLite/Postgres partial-index syntax only |
| **A tag has a life: `active` (on an item) -> `removed` (off, reusable) or `retired` (out of service for good)**. One row per link between a tag and an item | Staff can fix a wrong tagging and reuse the tag, while a damaged or lost tag is blocked forever; every link stays in the history | A tag UID appears in several rows over time |
| **Database guarantees**: one active tag per item *and* one active item per tag (partial unique indexes) | Cannot be broken even by code that bypasses the service (tested) | SQLite/Postgres partial-index syntax only |
| **Re-tagging needs explicit `replace`**; the old tag is kept in the history with its reason | Prevents silent overwrite; nothing is deleted (audit trail) | One more click |
| **Validate (dry run) then re-validate inside the real call** | Users see every problem at once; races are still caught | Two cheap checks |
| **Device first, then database; undo the device write if the save fails** | Never store an association the hardware did not accept; a reader cannot join a DB transaction, so compensation is the honest equivalent | Between the two steps a crash could leave a tag written but unsaved (visible as "carries ACC-x, not linked" on the pad) |
| **`tag_events` separate from the audit log** | Device sightings are high-volume; the audit log records who did what | Two tables with some overlap |
| **Simulator endpoints exist only outside production** and answer 404 in production | A "place a tag" button is a test tool, not a feature | Reviewers use it to demo hardware |
| **Simulator = one tag on the reader at a time in the UI** ("Generate new tag" replaces the tag on the pad; the API can still add several tags) | Matches a real staff station and stops tags piling up; several tags stay possible for bulk reads (inventory) | The UI hides multi-tag fields from the simulator buttons |
| **"Read reader" never changes anything**; simulator controls live in a collapsed box, with a one-click shortcut in the empty-reader message | Reads must be repeatable and identical in development and production; the real workflow stays uncluttered | One extra click to reach the simulator in general |
| **Taking a tag off an item is a choice: Remove (reusable) or Retire (permanent)**; the reader erases the tag first if it is on the reader. Never blocked by hardware | A tag that is still armed could set off the gate later, so we switch it off when we can; the tag may already be off the book or the reader down, so the library record is updated regardless and staff are told what still needs doing (amber message) | A retired tag can never come back; a removed one can be retired later (found damaged) |
| **Reusing a removed tag is one click**: the registry's "Reuse on another item" opens the tagging page with that exact tag on the (simulated) reader and selected (`/tagging?tag=UID`). On real hardware it asks staff to put the tag on the reader. A tag that is in use, retired or unknown is refused with a clear message | Removed tags are only worth keeping if they can actually be reused; real tags cost money across thousands of items | The simulator can hold any tag; hardware needs the tag physically present |
| **Re-tagging asks what happens to the old tag** (remove it or retire it); default is remove | The right answer depends on why the item is being re-tagged (wrong tag vs damaged tag) | One extra choice on the form |
| **Tag registry page + search through a tag's earlier items** | A stray tag can be traced to what it was, and removed/retired tags are not lost from view | A second list to keep consistent (it is computed from the same table) |
| **Tags that are in use or retired are shown but cannot be chosen**; removed tags can (they offer "Use this tag") | An in-use or retired tag can never be linked, so offering it only invites an error; a removed tag is exactly what staff want to reuse | None |
| **A single blank tag on the reader is auto-selected** | It is obviously the tag to use | Not applied when several blank tags are present |
| **Catalogue entry reuses the importer's validator** | Hand-typed and migrated data obey identical rules | `LIKE` search for now (full-text search is FR02 work) |

## Validation rules before tagging (`POST /api/rfid/tagging/validate`)
| Code | Meaning | HTTP when tagging |
|---|---|---|
| `item_not_found` | No such item | 404 |
| `item_status` | Item is lost or withdrawn | 409 |
| `title_missing` / `accession_missing` | Record incomplete | 409 |
| `tag_uid_invalid` | Not 8-32 hex characters (case, spaces, `:` and `-` are normalised) | 422 |
| `tag_bound_elsewhere` | Tag is active on another item (message names it) | 409 |
| `tag_already_on_item` | Already linked to this item | 409 |
| `tag_retired` | Tag was retired (out of service) and can never be used again | 409 |
| (warning) `tag_reused` | Tag was used before and removed; it will be linked again | - |
| `item_already_tagged` | Item has another tag and `replace` was not confirmed | 409 |
| (warning) `will_replace_old_tag` | `replace` is set; the item's current tag will be removed or retired (`old_tag_fate`) | - |

### The member side: before a patron card is linked (`POST /api/circulation/members/{no}/card/validate`)

FR05 asks for title **or member** records to be validated before tagging or re-tagging. `validate_member_tagging` in
`rfid/tagging.py` is the member-side twin of the item check above. The circulation desk (`POST .../members/{no}/card`), the
Administration page (`POST /api/admin/cards` with `kind = patron`) and the dry run all use it, so every path applies the same rules.

| Code | Meaning | HTTP when issuing |
|---|---|---|
| `no_such_member` | No member with that number | 404 (422 from the Administration page) |
| `name_missing` | The member record has no name to print on the card | 422 |
| `bad_card` | Not 6 to 64 letters and digits (case, spaces, `:` and `-` are normalised) | 422 |
| `card_in_use` | The card number is already registered (to a member or a member of staff) | 409 |
| (warning) `will_replace_old_card` | Re-tagging a member: their current card will be switched off | - |
| (warning) `member_blocked` | The card will identify the member, but loans stay refused until the block is lifted | - |

Device failures: reader offline -> 503 and nothing saved; write refused or tag not on the reader -> 409 and nothing saved; the
reader signals `error` (and `ok` on success). If the database fails after the device wrote, the write is cleared again.

## API
| Method | Path | Permission |
|---|---|---|
| GET | `/api/catalogue/items?q=&tagged=&status=&limit=&offset=` | `catalogue.read` |
| GET | `/api/catalogue/items/{id}` (with tag history) | `catalogue.read` |
| POST | `/api/catalogue/items` | `catalogue.write` |
| GET | `/api/rfid/readers` | `rfid.tag` |
| POST | `/api/rfid/readers/{id}/read` | `rfid.tag` |
| POST | `/api/rfid/tagging/validate`, `/api/rfid/tagging` | `rfid.tag` |
| POST | `/api/circulation/members/{no}/card/validate` (member-side dry run), `/api/circulation/members/{no}/card` | `member.write` |
| GET | `/api/rfid/tags/{uid}` (both directions + history) | `catalogue.read` |
| GET | `/api/rfid/tags?state=&q=&limit=&offset=` (the tag registry, with counts per state) | `catalogue.read` |
| POST | `/api/rfid/tags/{uid}/remove` (take off an item; reusable) and `/retire` (out of service for good; also works on an already removed tag). The response says what the reader did: `cleared`, `not_on_reader`, `reader_offline`, `device_error` or `not_linked` | `rfid.tag` |
| GET | `/api/rfid/events?tag_uid=&type=` | `rfid.tag` |
| POST/DELETE | `/api/rfid/mock/readers/{id}/tags`, `.../power` (simulator, non-production only) | `rfid.tag` |

Librarians and admins can tag; the circulation clerk can search and look up tags but not tag or create items (tested).

## Evidence
* 28 automated tests in `tests/test_tagging.py`, tagged AC02 / FR03 / FR05 and listed in `docs/traceability.md`.
* Safety behaviours were proven by **mutation checks**: removing the device rollback, or the re-tag confirmation, makes tests fail.
* UI walkthrough performed in a browser: add item (with invalid and duplicate input), place tag, check, link, re-tag with reason,
  reader offline, relationship and history view.

## Known limitations
* Only the staff reader exists; handheld inventory (AC05) and the gate (AC06) will add their own interfaces on the same base.
* Search is substring matching, not ranked full-text search.
* The simulator is in-memory: restarting the app clears the pad (the database keeps the saved associations).
* The security bit is stored and written, but nothing reads it yet; circulation (AC03) and the gate (AC06) will.
