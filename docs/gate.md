# Security gate, CCTV and notifications (AC06, FR07, FR09)

A gate mock reads every tag that passes. A tag whose security bit is still on and that is not on loan raises an alarm:
the accession number is recorded, a CCTV snapshot is taken, and a message is queued for each person who asked to be told.

## How passages reach the system (DR-18)

A gate driver implements `Gate.poll()`, which returns the passages since the last call. In production something must
call it, and there are three ways, all using `gate.service.poll_all` (every configured gate that is not a simulator):

| Way | How | When to use |
|---|---|---|
| The gate loop inside the application | `[gate] poll_seconds = 2` (0 = off); pauses during a restore | the normal set-up |
| A separate process | `python -m shelfsense.cli gate-poll --every 2` (once without `--every`; exit code 1 if a gate could not be read) | if the web application is restarted often, or runs on another machine |
| On demand | **Read the gates now** on Gate events, or `POST /api/gate/poll` (`gate.view`) | checking a gate after maintenance |

Use the loop **or** the separate process, not both: each passage is handed over once, to whoever reads it first. An offline
gate is reported (`ok: false` with the reason) and the other gates are still read. Simulator gates are never read here: in
development the simulator hands its passages over itself when someone walks through.

## How an alarm is decided

The alarm comes **only from the security bit read from the tag at the gate**. The gate never calls the library system
(the ILMS connector), so it keeps protecting the collection while the ILMS is down (FR07: "read the security bit while
the LMS is offline"; tested with the mock ILMS failing). The local catalogue and loans are used afterwards, only to give
the event its meaning.

| Bit read at the gate | Item | Result | Alarm |
|---|---|---|---|
| on | not on loan | `unauthorised`: someone is taking it out | **yes** |
| on | on loan | `armed_on_loan`: check-out could not switch it off | **yes** |
| on | tag belongs to no item | `unissued` | **yes** |
| off | on loan | `authorised`: a normal exit | no |
| off | not on loan | `bit_off`: switched off without a loan | warning |
| off | tag belongs to no item | `unissued` | warning |
| unreadable | any | treated as **on** (fail safe) | as above |

Each passage also records how many people went through (**footfall**), even when nobody carries a tag.

## What an alarm does

1. The **gate** sounds its siren (`indicate("alarm")`).
2. The **camera** takes a snapshot, stored with the event. The mock draws a labelled SVG; a real camera returns a JPEG or PNG.
3. One **notification** is queued per active (user, channel) preference. If nobody has asked for alerts, the event says so.
4. A **repeat alarm** for the same tag at the same gate within `duplicate_window_s` (60 s) is recorded and the siren sounds, but no second photo or message is made. Otherwise a gate that keeps ringing would flood everyone.

If the camera fails, the alarm is still recorded and announced, and the message says "No CCTV snapshot: ...".

## Notifications (provider-neutral)

- `NotificationProvider` interface with mock **email**, **SMS** and **print** providers. A real service is a class registered with `register_provider(channel, name, factory)` and chosen under `[notifications]`.
- **Per-user preferences:** each staff user ticks the channels they want and gives a contact (email address, phone number or printer name). Addresses are validated for the channel.
- **Queue, then deliver:** an alarm only adds rows (status `queued`), so a broken mail server can never delay or fail the alarm. Delivery is a separate step: the **Send queued messages** button, `POST /api/notifications/dispatch`, or `python -m shelfsense.cli notify-dispatch`.
- **Retries:** a failed send waits (`backoff_s` x attempts so far) and tries again, up to `max_attempts` (3), then the message is `failed`. A failed message can be retried by hand.

## Design decisions

| Decision | Why | Cost |
|---|---|---|
| Alarm from the tag's own bit, never from the ILMS | Works when the library system is down; the specification asks for exactly that | `armed_on_loan` shows when check-out could not switch the bit off |
| Gate events, passages and snapshots are append-only (DB triggers) | An alarm record must not be editable | Corrections are new records |
| Snapshot stored in the database, served with `nosniff`, a restrictive CSP and `sandbox`, and shown only in an image tag; text drawn on it is escaped | A stored image must never be able to run code in the browser (tested) | Fine for a demo; a real site would keep photos on disk or object storage |
| Queue first, deliver separately | Delivery trouble must not touch the event that caused the message | Needs the button, the CLI, or (later) a scheduled job |
| Mock providers keep an in-memory outbox | Shows what would have been sent | Lost on restart (the real provider has its own log) |

## API

| Method | Path | Needs |
|---|---|---|
| GET | `/api/gate/events`, `/events/{id}` (with its notifications), `/events/{id}/image`, `/summary`, `/devices` | `gate.view` |
| GET | `/api/notifications` (the queue) | `gate.view` |
| POST | `/api/notifications/dispatch`, `/{id}/retry` | `admin.config` |
| GET | `/api/notifications/outbox`, `/prefs` | `admin.config` |
| PUT | `/api/notifications/prefs/{user_id}` `{channels: {email|sms|print: {active, address}}}` | `admin.config` |
| read the gates | `POST /api/gate/poll` (needs `gate.view`): reads every real gate once and returns events and alarms per gate | |
| simulator | `POST /api/gate/mock/gates/{id}/walk`, `/mock/camera/fail` (need `rfid.tag`); `POST/DELETE /api/notifications/mock/fail` (needs `admin.config`); 404 in production | |

Configuration: `[gate] duplicate_window_s`, `[gate] poll_seconds`, `[notifications] email|sms|print`, `max_attempts`, `backoff_s`, and
`[[devices]]` entries with `kind = "gate"` and `kind = "camera"`.

## Demo data

`seed-demo` makes the admin an email recipient (`security@library.example`) and the librarian an email
(`head-librarian@library.example`) and print (`front-desk`) recipient. All addresses are synthetic `.example` values.

## Known limits

- Mock gate and camera only. Gate hardware that pushes events would need a small driver that feeds `process`.
- Gates are read by polling (`Gate.poll()`); a gate that pushes events needs a driver that queues them for `poll()` to return.
- SMS and print are mock providers. Patron notices (overdue, holds) are not built yet; they would use the same queue.
- Recipients are staff users only.
- Acknowledging or closing an alarm (who handled it, and when) is not built.
