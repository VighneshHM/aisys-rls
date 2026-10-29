# Patron loan reminders and the notification loop (FR09)

A member with an e-mail address or phone number is told **one day before** a loan is due and **again on the due day**.
A background loop finds the reminders that are due and delivers them, so nobody has to press a button.

## The rules

| Situation | What happens |
|---|---|
| Loan due tomorrow (UTC day) | One **"due tomorrow"** message per channel the member has |
| Loan due today | One **"due today"** message per channel |
| Due in 2 or more days, already overdue, or returned | Nothing. (The days before are configurable, see below.) |
| Member has an e-mail | An e-mail. A phone number gets an SMS (160 characters at most). Both get both. |
| No e-mail or phone, or reminders switched off for the member | Nothing is sent; the scan counts them as `no_contact` / `opted_out` |
| Blocked member | Still reminded: the reminder is about the loan |
| Loan renewed (new due date) | New reminders for the new due date; any not yet sent for the old date are **cancelled** |
| Item returned before the reminder goes out | The queued reminder is **cancelled**, not sent |
| The e-mail server is down | Retried with back-off like every queued message, then marked failed (and can be retried by hand) |

Days are whole days in UTC (assumption A-20). A loan due at 00:30 on the 10th is "due tomorrow" for the whole of the 9th.

## Exactly once

Every reminder is a row in the notification queue naming its loan, kind, channel and due date. A database index makes that
combination unique, so scanning every minute, restarting the app, or running the loop in two places can never send one
twice. (Tested with four scanners at the same moment.) Reminders are one message per loan, so a member with three items due
today gets three messages.

## Where the contact details live

On the member: e-mail, phone and a "send reminders" switch (migration `0009`). Staff with the `member.write` permission set them
on **Circulation**, under the member, with **Contact and reminders** (or when adding the member). The form checks the
formats (phone numbers are tidied, so `+44 (1234) 567-890` is stored as `+441234567890`). The audit log records that a
contact detail was set or cleared, never the address itself. The member's panel shows the reminders they were sent.

## Running the loop

Three ways, all doing the same pass (queue due reminders, then deliver what is queued):

1. **Inside the running app** (the easiest, and what an installed copy uses). In `config/config.toml`:
   ```
   [notifications]
   reminders = true
   remind_days_before = 1          # 1 to 30
   auto_dispatch_seconds = 60      # 0 = off
   ```
   The loop starts and stops with the application, survives any error in a pass (logged, tried again next time) and does
   nothing while a restore is running.
2. **On its own:** `python -m shelfsense.cli notify-dispatch --every 60` (Ctrl+C stops it). Without `--every` it does one pass.
   Use this if the app runs under a service manager and you prefer a separate scheduled process.
3. **By hand:** the **Send queued messages** button on *Gate events*, `POST /api/notifications/dispatch`, and
   `POST /api/notifications/reminders/run` (administrators) which only queues the due reminders.

Running two of these at once is safe. The unique index keeps a reminder single, and each queued message is **claimed**
(an atomic update of its attempt counter) before it is sent, so only one dispatcher wins it: tested with three at the same moment.
If the app stops after a claim and before the send, the message is simply tried again.

A pass delivers the whole backlog, not 100 messages a minute: a day's worth of 3,000 reminders is delivered in one pass of
about 14 seconds (at most 10,000 messages per pass, so one pass cannot run for ever). The interval has a floor of one second.

## Trying it (development)

`seed-demo` gives three demo patrons synthetic contact details (`.example` addresses and reserved test numbers). On
**Circulation**, choose a member with a loan and use **Due tomorrow** or **Due today** beside the loan (development only, 404
in production), then watch the member's *Reminders sent* list and the **Gate events** queue. With `auto_dispatch_seconds` set
the messages go out within that many seconds; the mock providers record them (`/api/notifications/outbox`).

## Safety of the text

Titles come from spreadsheets and can hold anything. Line breaks are removed before a title reaches a subject line (so it can
never add a header such as `Bcc:` when a real mail gateway is used), e-mail and phone formats must match the whole value (a
trailing newline is not accepted), and the pages show all of it as plain text.

## Design decisions

| Decision | Why | Cost |
|---|---|---|
| Reuse the existing queue, retries and provider adapters | Reminders get retries, failure handling and the mock/real driver split for free | The queue table gained a few columns |
| A scan creates the messages; delivery is separate | A mail outage never blocks or loses a reminder | Two steps in a pass |
| Uniqueness enforced by the database, not by code | Correct however many schedulers run | A conflict is caught and counted, not an error |
| Cancel at delivery time, not at return time | Works however the loan ended (return, renewal, import) with no hooks in circulation | The queued row stays, marked cancelled |
| Loop inside the app, optional, plus a CLI mode | An installed copy needs no extra scheduler; a separate process is still possible | A thread in the web process |
| Whole UTC days | Same rule as the reports; no time-zone surprises | A library far from UTC sees reminders at UTC midnight |

## Known limits

- Reminders are not sent for overdue items (the scope was: the day before and the due day). A repeated overdue notice is a
  small extension of the same scan.
- Only e-mail and SMS reach patrons, through mock providers here; a real gateway is a class registered under a driver name.
- A reminder is created when the loop runs. If the app is down for the whole of a day, that day's reminder is not sent later
  (the "due today" one still goes out if the loop runs on the due day).
- One message per loan, not a combined digest.
- No patron-side preferences (language, quiet hours); staff set a member's contact details.
