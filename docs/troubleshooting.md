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
| Gate alert not e-mailed | Alerts are queued first | **Gate events** then **Send queued messages**, or schedule `notify-dispatch` (`docs/deployment.md`) |
| A patron got no reminder | No e-mail or phone on the member, reminders switched off for them, `auto_dispatch_seconds = 0` with no scheduler, or the loan is not due tomorrow or today | **Circulation**, the member's *Reminders* line and **Contact and reminders**; see `docs/reminders.md` |
| A reminder shows "cancelled" | The item was returned or renewed before it went out | Nothing to do; this is intended |
| Update refused: "running" | The application is up | Stop it, then update |
| Update refused: "altered" or "trusted key" | Damaged copy, or signed by someone else | Copy the package again; check you have the right vendor key |
| Install: "too deep for Windows" | Folder path over the Windows limit | Install into a short folder such as `C:\aisys` |
| An update was interrupted | Power loss or the tool was killed | `python manage.py recover` |
| The new version misbehaves after an update | | `python manage.py rollback --yes` (see `docs/deployment.md`) |
| A change does not show up | The application was not restarted after a code or config change | Restart the application |

**Where to look:** `/health` and `/health/ready` (database and schema revision), the structured JSON log on the console, the
**Audit log** for who did what, `python manage.py status` for versions and history, and the **Updates** page.
