# SIP2 connector (FR03, AC03)

The application can talk to a library system (ILMS) over the **SIP2 protocol** (3M Standard Interchange Protocol, version 2.00).
It sits behind the existing connector boundary (`CirculationConnector`, `docs/circulation.md`): nothing else in the
application changed, and `driver = "mock"` still works as before.

**What is and is not claimed.** The SIP2 client was written from the specification and has been tested **only against the
simulator in this repository** (`python -m aisys.cli sip2-sim`), which was also written from the specification. It has **not**
been tested against, or certified by, any vendor ILMS. See "Limits" below.

Code: `src/aisys/circulation/sip2/` (`codec.py`, `client.py`, `adapter.py`, `server.py`).

## What is implemented

| Layer | File | What it does |
|---|---|---|
| Codec | `codec.py` | Pure encode and decode of messages, fixed and variable fields, the optional error-detection trailer, the 18-character date. Only `Sip2Error` ever comes out of decoding (fuzz-tested) |
| Client | `client.py` | One TCP session, opened when first needed; logs in on connect; one request at a time; sequence digit per message; resend handling |
| Adapter | `adapter.py` | `Sip2Connector`, driver name `sip2`: maps the connector operations to messages and the ILMS answers to the application's neutral result codes |
| Simulator | `server.py` | `Sip2Server`: a SIP2 server with its own patrons, items and loans, optional login and failure injection |

Messages:

| Request (application to ILMS) | Answer | Used for |
|---|---|---|
| `93` Login | `94` | Opening a session (when a login user is configured) |
| `99` SC status | `98` ACS status | `aisys doctor`: is the ILMS online |
| `23` Patron status | `24` | `patron_status`; the reason for a refusal (blocked, fines, limits) |
| `63` Patron information | `64` | Listing the items charged to a member (verifying a checkout after a lost answer) |
| `11` Checkout | `12` | `checkout` |
| `09` Checkin | `10` | `checkin` |
| `29` Renew | `30` | `renew` |
| `17` Item information | `18` | `due_date`; due date when the ILMS leaves it out; verifying a checkin or renewal |
| `97` Request resend | the last answer again | A damaged answer |
| `96` Resend (from the ILMS) | - | The ILMS did not understand: the same message is sent again |

Variable fields handled: `AO AA AB AC AE AF AG AH AJ AP AQ AU BL BV CN CO CP CQ`.

Wire rules followed: one line ending in a carriage return; fixed fields in the specified order; variable fields as a two-letter id,
the value and `|`; optional error detection `AY<sequence digit>AZ<4 hex digits>` (a 16-bit checksum defined so the sum of all
characters plus the checksum is zero modulo 65536); frames longer than 65536 bytes refused; ASCII only; a value containing `|`, a
control character or a non-ASCII character is refused before sending; a wrong checksum on an answer is refused.

## How it maps to the connector operations

| Connector operation | Messages | Notes |
|---|---|---|
| `patron_status` | `23` / `24` | Blocked or invalid patron, or excessive fines/fees flags, become refusals |
| `checkout` | `11` / `12` | Sends member number (`AA`) and item accession number (`AB`). If the answer has no due date, asks with `17`; if the ILMS still gives none, the local loan period is used |
| `renew` | `29` / `30` | Same due-date handling |
| `checkin` | `09` / `10` | Sends the location (`AP`). If the ILMS says the item is not checked out, the local loan is closed anyway (the ILMS already had it back, for example from a return at another desk) and the history says so; a renewal in that situation is still refused |
| `due_date` | `17` / `18` | Circulation status `04` means charged; anything else is "not on loan" |

The institution id (`AO`) comes from `[ilms] institution`; the location (`CP`, `AP`) from `[ilms] location`. Members and items are
identified by the member number and the accession number the application already holds.

## Configuration

`config/config.toml`:

```toml
[ilms]
driver = "sip2"
endpoint = "192.168.1.50"   # host name or IP address of the ILMS (required for sip2)
port = 6001                 # the ILMS's SIP2 port (required for sip2)
login = "aisys"             # SIP2 login user (CN); leave out if the ILMS needs no login
institution = "LIBRARY"     # institution id (AO)
location = "MAIN"           # location code of this system (CP, AP)
timeout_s = 5               # seconds to wait for an answer, 0.1 to 120
checksum = true             # error detection (sequence digit and checksum); turn off only if the ILMS rejects it
retries = 2                 # attempts after the first, made by the resilience wrapper
backoff_s = 0.1             # pause between attempts
```

There is no `protocol` setting: SIP2 is the only protocol (DR-13). An older file that still has `protocol = "ncip"` stops the
application at start with a plain message; delete the line (D-059).

The login **password is never read from the file** (a password written there is ignored). Set it in the environment of the
application:

```powershell
$env:AISYS_ILMS_PASSWORD = "the-password"
```

A `sip2` driver without `endpoint` or `port` stops startup with a plain message. `timeout_s` outside 0.1 to 120 also stops startup.

Loan rules belong to the ILMS when `sip2` is used. The Settings page's loan rules still govern the application's own local
checks (reference item, blocked member, fines over the limit, item already out), which run before the ILMS is asked.

## Trying it with the simulator

The simulator needs no real library system. In a first terminal:

```powershell
python -m aisys.cli sip2-sim --port 6101 --user aisys --password demo
```

Omit `--user` and `--password` for a server that needs no login. By default it listens on 127.0.0.1 and accepts any member or item
number it has not seen (so it works behind the demo data with no set-up). Demo steps, in a second terminal:

1. Set the password: `$env:AISYS_ILMS_PASSWORD = "demo"`.
2. In `config/config.toml` set `[ilms]` to `driver = "sip2"`, `endpoint = "127.0.0.1"`, `port = 6101`, `login = "aisys"`.
3. Check the connection: `python -m aisys.cli doctor`. The ILMS line should read OK, "SIP2 at 127.0.0.1:6101 answered and is online".
4. Start the application (`python -m aisys.cli run`) in the same terminal, sign in, open **Circulation** and check out a book to a
   member. The history shows the ILMS transaction; the simulator holds the loan.
5. Try a renewal and a check-in. Stop the simulator and try again: the clerk is told the library system is not answering and nothing is
   half-recorded.

Failure injection (`fail_next` with `drop`, `lost_reply`, `silent` or `garble`) is available from Python (`Sip2Server`) and is what
the tests use; the command-line simulator does not expose it.

## Retries and verification

SIP2 has no transaction id, so repeating a request after a lost answer could lend or renew twice. The connector therefore
**remembers its answers by the application's transaction id** (a repeat of a finished request gets the same answer,
`replayed = true`) and, when a request was sent but no usable answer arrived, **checks the ILMS before sending it again**.

| What happened | What the connector does | Result |
|---|---|---|
| Checkout sent, answer lost | On the retry, asks `63` for the member's charged items. If the item is there, the first try worked: no second checkout. Otherwise sends `11` again | Never lent twice |
| Checkin sent, answer lost | On the retry, sends `09` again; if refused, asks `17`: an item no longer charged means the first try worked | Success reported once |
| Renewal sent, answer lost | On the retry, asks `17`: a due date different from the one held means the first try worked | Never renewed twice |
| Connection dropped or no answer within `timeout_s` | The session is closed; the next request connects and logs in again. The request counts as "sent without an answer" (`Sip2Unsure`, a timeout), so the above checks apply | Staff can try again |
| Answer damaged (wrong checksum, bad sequence, malformed) | Sends `97` to ask for the answer again, up to 2 times; then gives up as "sent without an answer" | Same as above |
| ILMS answers `96` (did not understand) | Sends the same message again, up to 2 times | Same as above |
| Cannot connect, or login refused | Nothing was sent, so it is an ordinary connector error that is safe to retry | Clerk told the ILMS is unavailable |

After the connector gives up, the resilience wrapper (`retries`, `backoff_s`) repeats with the same transaction id, then the
application reports `ilms_unavailable` (HTTP 503) and records nothing local.

## Error mapping

The refusal text (screen message `AF`, print line `AG`) is read for keywords first, in the order below; if none matches, the
patron status flags are asked for with `23`; if nothing explains it, the code is `ilms_refused`.

| Code | Keywords in the ILMS text | Patron status flag (when no keyword matched) |
|---|---|---|
| `not_on_loan` | not checked out, not charged, not on loan, not loaned (for checkin, also an empty message) | - |
| `renewal_limit` | (renew only) renew, too many, limit, maximum | too many renewals |
| `fines_exceeded` | fine, fee, owe, outstanding | excessive fines or fees |
| `patron_blocked` | block, privilege, barred, suspend, expired, invalid patron, not allowed, lost card | patron invalid, charge privileges denied, card reported lost |
| `item_on_loan` | already, checked out, charged to, on loan | - |
| `loan_limit` | too many, limit, maximum | too many items charged |
| `ilms_refused` | anything else | - |
| `ok` | - | - |

## Security notes

- SIP2 itself has **no encryption**: user name, password and patron data cross the network in clear text. Put a VPN, stunnel or a
  TLS-terminating proxy between the application and the ILMS, or keep them on an isolated network segment (`docs/site-addressing.md`).
- The password comes only from the environment variable `AISYS_ILMS_PASSWORD`; it is not stored in the configuration file, not
  written to the log and not shown by `aisys doctor`.
- Everything read from the network is treated as hostile: size limit, ASCII only, checksum verified, and every malformed input ends as
  `Sip2Error` (fuzz-tested). Values sent are refused if they contain `|`, control characters or non-ASCII characters, so a member
  or item number cannot inject extra fields.
- The error-detection checksum protects against damage, not against an attacker.
- Patron passwords (PIN) are not sent.

## Limits

- Tested only against the simulator (written from the specification). **Not certified, and not tested against any vendor ILMS.**
- A subset of SIP2: no hold, fee-paid, block-patron, end-session or status-update messages; patron passwords not sent; ASCII only.
- Vendors vary in field use and wording. The keyword lists in `adapter.py` may need small changes for a given system.
- The checksum follows the specification's definition and is verified by property tests and hand-worked examples, not against a
  published vendor trace.
- **NCIP 2.0 is not implemented.** The SOP names SIP2 or NCIP; only SIP2 is built.
- One session and one request at a time (a busy site would queue circulation requests).

## Adapting to a real ILMS

1. Run `aisys doctor` against it (connect, log in, status). Ask the ILMS owner for the SIP2 account, the institution id and the
   location code, and whether it needs the checksum.
2. Try a checkout, renew and check-in with test members and items; compare the ILMS's own log with the history.
3. Where the ILMS words refusals differently, extend the keyword tuples at the top of `src/aisys/circulation/sip2/adapter.py`
   (`_PATRON`, `_FINES`, `_ON_LOAN`, `_LIMIT`, `_NOT_OUT`) and the patron status flag handling in `_reason_from_patron`.
4. Where a field differs (for example how the due date or item identifier is sent), adjust `_fields` and the message-building
   methods in `adapter.py`; message layouts are in `codec.py` (`FIXED_LENGTH`).
5. Add a test with the simulator for each change. For NCIP, write another class implementing `CirculationConnector` and register it
   under a new driver name in `src/aisys/circulation/registry.py`, the same way `sip2` is registered.
