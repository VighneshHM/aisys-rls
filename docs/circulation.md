# Circulation (AC03, FR04) and the ILMS connector

Staff check out, renew and check in items. Every action goes to the library system (ILMS) through a **connector
interface**, and every attempt is written to an append-only history.

## Design decisions

| Decision | Why | Cost |
|---|---|---|
| **One interface, `CirculationConnector`** (`patron_status`, `checkout`, `renew`, `checkin`, `due_date`). The application never calls an ILMS directly | The specification asks for an "NCIP 2.0 adapter / SIP2 adapter *boundary*" with mocks where vendor endpoints are unavailable. A real adapter is a new class registered under a driver name; nothing else changes. A SIP2 adapter now exists (`driver = "sip2"`, `docs/sip2.md`) | Only SIP2 is built, and it is tested against a simulator only (no certification claimed). NCIP 2.0 is not built |
| **Results are protocol-neutral** (`ok`, `code`, `message`, `due_at`); the mock answers in SIP2 wording, and the page shows the protocol the connector actually speaks | Callers should not care how an ILMS words a refusal; another protocol would map its replies to the same codes. A setting that asked the mock for NCIP wording was removed (D-059) | Real adapters must map their replies to our codes (the SIP2 mapping is in `docs/sip2.md`) |
| **The ILMS owns loan policy** (loan period, renewal limit, loan limit). We check only what our own records know first: reference item, blocked member, fines over the limit, item already out | A real ILMS is the authority on rules; our local checks give a fast, explained refusal for the common blocks | In the demo the mock gets its limits from our config (it has no database of its own) |
| **Same transaction id on every retry; the ILMS repeats its answer for a known id** | A lost reply must not lend the book twice (tested) | A real ILMS needs to support idempotent requests, or the adapter must emulate it. SIP2 has no transaction id, so the SIP2 adapter remembers answers by id and checks the ILMS before repeating a request whose answer was lost |
| **Bounded retries, then `ilms_unavailable`; nothing local changes** | Staff are told clearly and can try again; no half-done loan | No offline queue yet (planned with FR12 offline lifecycle) |
| **Security bit follows the loan**: switched off at check-out, back on at check-in; best effort | A book that leaves with its bit on sets off the gate. But a missing or offline reader must never stop the desk, so the loan goes through and staff get an amber note saying what to do | Needs the tag on the reader; otherwise manual follow-up |
| **Every attempt is recorded**: done, refused (local or ILMS) or failed (ILMS down), with the ILMS transaction id | "Auditable transaction history" and traceability to the ILMS's own log | Refusals also add rows |
| **History is append-only (DB triggers)** and an item can have **one open loan** (partial unique index) | Cannot be broken even by code bypassing the service (tested) | SQLite syntax; the same ideas exist in PostgreSQL |
| **Patron card = identify only**. It shows the member; it creates no session | Specified by the project owner | - |
| **Timestamps always carry a UTC offset in the API** (`timeutil.iso`) | SQLite returns bare timestamps and browsers read those as local time, which shifted displayed times; fixed for every endpoint | - |

## Fines

| Rule | Default | Config key |
|---|---|---|
| Days late = whole 24-hour periods past the due time | | |
| Grace period: this many days late cost nothing | 1 | `grace_days` |
| Fine per billable day | 0.25 | `fine_per_day_cents` |
| Cap for one loan | 10.00 | `max_fine_per_loan_cents` |
| Circulation stops when the balance **exceeds** | 5.00 | `fine_limit_cents` |

- **Charged when an overdue item is returned or renewed**, in the same commit. A renewal restarts the clock, so it charges the fine so far first; otherwise renewing a late book would erase its fine. While an item is still out, staff see the fine so far, but it is not on the balance yet.
- **Ledger, not just a number:** every change is a row in the append-only `fine_entries` table (`opening`, `charge`, `payment`, `waiver`), with who did it and why. The member's balance always equals the ledger (tested). Existing stored balances became an `opening` entry (migration 0007).
- **Staff with `member.write`** can record a payment or waive a fine (a reason is required). Neither can exceed the balance. Both are also written to the audit log.
- Paying the balance down to the limit or below lets the member borrow again.

## Rules

| Situation | Where decided | Code | HTTP |
|---|---|---|---|
| Reference item | local | `reference_item` | 409 |
| Item damaged, lost or withdrawn | local | `item_unavailable` | 409 |
| Item already on loan | local (and DB index) | `item_on_loan` | 409 |
| Member blocked | local, then ILMS | `patron_blocked` | 409 |
| Fines **over** `fine_limit_cents` | local, then ILMS | `fines_exceeded` | 409 |
| Too many loans (`max_loans`) | ILMS | `loan_limit` | 409 |
| Renewed too often (`max_renewals`) | ILMS | `renewal_limit` | 409 |
| Renew or check in an item not on loan | local | `not_on_loan` | 409 |
| ILMS did not answer after the retries | connector | `ilms_unavailable` | 503 |

A blocked member can still return items. A renewal by a blocked member is refused.

## Configuration (`config/config.toml`)

```toml
[circulation]
fine_limit_cents = 500   # a member owing MORE than this cannot borrow
loan_days = 14
max_renewals = 2
max_loans = 5

[ilms]
driver = "mock"          # name of a registered connector driver
retries = 2
backoff_s = 0.1
```

For the SIP2 driver (`driver = "sip2"`) add `endpoint`, `port`, `login`, `institution`, `location`, `timeout_s` and `checksum`; the
login password is read only from the environment variable `SHELFSENSE_ILMS_PASSWORD`. `shelfsense doctor` connects, logs in and reports the
ILMS status (OK, WARN when it says it is offline, FAIL when it cannot be reached or refuses the login). To try it without a real
system run `python -m shelfsense.cli sip2-sim --port 6101 --user shelfsense --password demo`. Full reference: `docs/sip2.md`. When SIP2 is
used the ILMS owns the loan rules; the Settings page's loan rules still govern the local checks.

To connect another kind of system: implement `CirculationConnector` (see `src/shelfsense/circulation/connector.py`), call
`register_driver("my-ilms", MyAdapter)` and set `driver = "my-ilms"`.

## API (`/api/circulation`)

| Method | Path | Needs |
|---|---|---|
| GET | `/members?q=` find members by name or number | `member.read` |
| POST | `/members` `{name, member_no?, card_uid?}` add a member (the number is automatic if empty) | `member.write` |
| POST | `/members/{member_no}/card` `{uid}` issue a patron card; a card they had before is switched off | `member.write` |
| POST | `/members/identify` `{uid}`: patron card scan, returns the member and their loans | `member.read` |
| GET | `/members/{member_no}` | `member.read` |
| POST | `/members/{member_no}/block` `{blocked, reason}` | `member.write` |
| POST | `/checkout` `{member_no, item_ref}` | `circulation.checkout` |
| POST | `/renew` `{item_ref}` | `circulation.renew` |
| POST | `/checkin` `{item_ref}` | `circulation.checkin` |
| GET | `/due?item_ref=` due-date enquiry (asks the ILMS; not written to the history) | any circulation permission |
| GET | `/items/resolve?ref=` an item by accession no., barcode or tag UID | any circulation permission |
| GET | `/members/{member_no}/fines` the ledger and balance | `member.read` |
| POST | `/members/{member_no}/fines/payment` and `/waiver` `{amount_cents, note}` | `member.write` |
| GET | `/loans`, `/events` (history; filter by `member_no`, `item_ref`) | `member.read` or `audit.read` |
| simulator | `/mock/patron-cards`, `/mock/present`, `/mock/ilms/fail` (404 in production) | any circulation permission |

`item_ref` is an accession number, a barcode or the UID of the item's active tag.

## Members

Staff with `member.write` can **add a member** (name; the number is the next free `M####` unless given; a card is optional), **issue or replace a card** (the old card is switched off and refused with "replaced or withdrawn"), and **find a member by name or number** for a patron who has no card with them. The member panel shows the member's current card (and how many earlier cards were withdrawn), or "No card issued yet". Card numbers are unique across staff and patron cards, so a staff card can never become a patron card. All of it is audit-logged.

## Demo members (synthetic)

| Card | Member | Situation |
|---|---|---|
| PATRON000001 / 2 / 6 | Priya Nair / Tom Becker / Kim Park | can borrow |
| PATRON000003 | Lena Okafor | blocked |
| PATRON000004 | Sam Ortiz | owes 12.50, over the 5.00 limit |
| PATRON000005 | Ana Silva | owes 3.00, under the limit, can borrow |

## Known limits

- SIP2 is implemented (a subset) but tested only against a simulator; no vendor ILMS was used and no certification is claimed. NCIP 2.0 is not implemented. SIP2 is unencrypted: use a tunnel.
- Fines follow one simple rule (see Fines above): no per-item or per-member rates, no lost-item replacement charge, no refunds.
- The mock ILMS keeps no loan database; it applies policy to the request it receives and remembers answers by transaction id in memory (lost on restart).
- No holds or reservations; no notices (FR09) yet; no offline queue for when the ILMS is down.
- The security bit is only changed when the tag is on the reader; there is no later "verify" read.
