# Troubleshooting (NFR07, NFR08)

| Symptom | Likely cause | What to do |
|---|---|---|
| Staff card "Unknown card" | The card was never issued, or the app was started with a different `AISYS_SECRET_KEY` than the database was created with | Issue the card in **Administration**; keep the key stable (an installed copy uses `config/secret.key`) |
| Staff card "Card or account is disabled" | The person or the card was deactivated | **Administration**: activate the person or reactivate the card |
| Account locked after wrong passwords | Failed-login limit reached | **Administration**: Unlock, or wait for the lockout to end |
| A patron card cannot sign in | By design: a patron card only identifies a member | Sign in with a staff card |
| "The library data is being restored" (503) | A restore is running | Wait a few seconds and retry |
| Restore says "database stayed busy" | Another process holds the database | Stop the other process and run it again; nothing was changed |
| Import shows problems or duplicates | Bad rows in the spreadsheet | Download the error CSV, fix, upload again; a batch can be rolled back |
| Check-out refused | Member blocked, fines over the limit, loan limit, or item status | The message names the reason; see `docs/circulation.md` |
| "The library system is not answering": check-out, renew or check-in fails with "try again" (503) when `[ilms] driver = "sip2"` | The ILMS is down, the network or tunnel to it is broken, the login was refused, or it is slow. Run `python -m aisys.cli doctor` and read the ILMS line: **"cannot connect to the ILMS at host:port"** means nothing is listening there (wrong `endpoint` or `port`, ILMS or tunnel not running, firewall); **"the ILMS did not answer"** means it accepted the connection but did not reply within `timeout_s` (overloaded, wrong port, or something that is not a SIP2 server; try a longer `timeout_s`); **"the ILMS refused the login"** means the user or password is wrong (`[ilms] login`, environment variable `AISYS_ILMS_PASSWORD`); **"answered but reports it is offline"** (WARN) means its SIP2 service is up but its library system is not. After fixing it, try again: nothing was recorded locally, and a request whose answer was lost is checked with the ILMS first, so a book is not lent or renewed twice. The history shows the failed attempt. See `docs/sip2.md` |
| Gate alert not e-mailed | Alerts are queued first | **Gate events** then **Send queued messages**, or schedule `notify-dispatch` (`docs/deployment.md`) |
| A patron got no reminder | No e-mail or phone on the member, reminders switched off for them, `auto_dispatch_seconds = 0` with no scheduler, or the loan is not due tomorrow or today | **Circulation**, the member's *Reminders* line and **Contact and reminders**; see `docs/reminders.md` |
| A reminder shows "cancelled" | The item was returned or renewed before it went out | Nothing to do; this is intended |
| A search finds nothing for a word you know is there | The index finds whole words and the start of a word; a piece from the middle of a word is found only when nothing else matches | Type the start of the word, or a longer piece |
| Search says it has no full-text index | This SQLite was built without FTS5 | Search still works (plain matching, slower); `python -m aisys.cli doctor` says so |
| The public catalogue says "Too many searches" | One address made more than 60 searches in a minute | Wait a minute |
| "No card printer is configured" or "The card was not printed" | No `card_printer` device, or it is jammed or offline | Check `[[devices]]` and the printer; the failure is in the audit log |
| An order cannot be booked in | More received than ordered, the order is cancelled, or an accession number clashes | The message says which; nothing is booked in on a failure |
| A label barcode is blank | The barcode value has characters a barcode cannot hold | The accession number is used instead; check the item's barcode |
| `doctor` reports FAIL | A folder is not writable, a TLS file is missing, or the production key is short | The line names the item; fix it and run `doctor` again |
| Update refused: "running" | The application is up | Stop it, then update |
| Update refused: "altered" or "trusted key" | Damaged copy, or signed by someone else | Copy the package again; check you have the right vendor key |
| Install: "too deep for Windows" | Folder path over the Windows limit | Install into a short folder such as `C:\aisys` |
| An update was interrupted | Power loss or the tool was killed | `python manage.py recover` |
| The new version misbehaves after an update | | `python manage.py rollback --yes` (see `docs/deployment.md`) |
| A change does not show up | The application was not restarted after a code or config change | Restart the application |

**Where to look:** `/health` and `/health/ready` (database and schema revision), the structured JSON log on the console, the
**Audit log** for who did what, `python manage.py status` for versions and history, and the **Updates** page.
