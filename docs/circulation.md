# Circulation (AC03, FR04) and the ILMS connector

Staff check out, renew and check in items. Every action goes to the library system (ILMS) through a **connector
interface**, and every attempt is written to an append-only history.

## Design decisions

| Decision | Why | Cost |
|---|---|---|
| **One interface, `CirculationConnector`** (`patron_status`, `checkout`, `renew`, `checkin`, `due_date`). The application never calls an ILMS directly | The SOP asks for an "NCIP 2.0 adapter / SIP2 adapter *boundary*" with mocks where vendor endpoints are unavailable. A real SIP2 or NCIP adapter is a new class registered under a driver name; nothing else changes | The wire protocols themselves are not built (nor claimed: the SOP forbids unevidenced protocol claims) |
| **Results are protocol-neutral** (`ok`, `code`, `message`, `due_at`); the mock words messages as SIP2 or NCIP (`[ilms] protocol`) | Both protocols report the same facts differently (SIP2 screen message, NCIP problem); callers should not care | Real adapters must map their replies to our codes |
| **The ILMS owns loan policy** (loan period, renewal limit, loan limit). We check only what our own records know first: reference item, blocked member, fines over the limit, item already out | A real ILMS is the authority on rules; our local checks give a fast, explained refusal for the common blocks | In the demo the mock gets its limits from our config (it has no database of its own) |
| **Same transaction id on every retry; the ILMS repeats its answer for a known id** | A lost reply must not lend the book twice (tested) | A real ILMS needs to support idempotent requests, or the adapter must emulate it |
| **Bounded retries, then `ilms_unavailable`; nothing local changes** | Staff are told clearly and can try again; no half-done loan | No offline queue yet (planned with FR12 offline lifecycle) |
| **Security bit follows the loan**: switched off at check-out, back on at check-in; best effort | A book that leaves with its bit on sets off the gate. But a missing or offline reader must never stop the desk, so the loan goes through and staff get an amber note saying what to do | Needs the tag on the reader; otherwise manual follow-up |
| **Every attempt is recorded**: done, refused (local or ILMS) or failed (ILMS down), with the ILMS transaction id | "Auditable transaction history" and traceability to the ILMS's own log | Refusals also add rows |
| **History is append-only (DB triggers)** and an item can have **one open loan** (partial unique index) | Cannot be broken even by code bypassing the service (tested) | SQLite syntax; the same ideas exist in PostgreSQL |
| **Patron card = identify only**. It shows the member; it creates no session | Specified by the owner of the exercise | - |
| **Timestamps always carry a UTC offset in the API** (`timeutil.iso`) | SQLite returns bare timestamps and browsers read those as local time, which shifted displayed times; fixed for every endpoint | - |

## Rules

| Situation | Where decided | Code | HTTP |
|---|---|---|---|
| Reference item | local | `reference_item` | 409 |
| Item lost / withdrawn | local | `item_unavailable` | 409 |
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
protocol = "sip2"        # wording the mock uses: sip2 | ncip
retries = 2
backoff_s = 0.1
```

To connect a real system: implement `CirculationConnector` (see `src/aisys/circulation/connector.py`), call
`register_driver("my-ilms", MyAdapter)` and set `driver = "my-ilms"`.

## API (`/api/circulation`)

| Method | Path | Needs |
|---|---|---|
| POST | `/members/identify` `{uid}`: patron card scan, returns the member and their loans | `member.read` |
| GET | `/members/{member_no}` | `member.read` |
| POST | `/members/{member_no}/block` `{blocked, reason}` | `member.write` |
| POST | `/checkout` `{member_no, item_ref}` | `circulation.checkout` |
| POST | `/renew` `{item_ref}` | `circulation.renew` |
| POST | `/checkin` `{item_ref}` | `circulation.checkin` |
| GET | `/due?item_ref=` due-date enquiry (asks the ILMS; not written to the history) | any circulation permission |
| GET | `/items/resolve?ref=` an item by accession no., barcode or tag UID | any circulation permission |
| GET | `/loans`, `/events` (history; filter by `member_no`, `item_ref`) | `member.read` or `audit.read` |
| simulator | `/mock/patron-cards`, `/mock/present`, `/mock/ilms/fail` (404 in production) | any circulation permission |

`item_ref` is an accession number, a barcode or the UID of the item's active tag.

## Demo members (synthetic)

| Card | Member | Situation |
|---|---|---|
| PATRON000001 / 2 / 6 | Priya Nair / Tom Becker / Kim Park | can borrow |
| PATRON000003 | Lena Okafor | blocked |
| PATRON000004 | Sam Ortiz | owes 12.50, over the 5.00 limit |
| PATRON000005 | Ana Silva | owes 3.00, under the limit, can borrow |

## Known limits

- No wire-level SIP2 / NCIP, and no certification claim.
- Fines are a stored amount (no overdue-fine calculation yet). Overdue loans are flagged, not charged.
- The mock ILMS keeps no loan database; it applies policy to the request it receives and remembers answers by transaction id in memory (lost on restart).
- No holds or reservations; no notices (FR09) yet; no offline queue for when the ILMS is down.
- The security bit is only changed when the tag is on the reader; there is no later "verify" read.
