# Administrator's guide (FR11, NFR07, NFR08)

This is the one document an administrator needs to run ShelfSense day to day. It covers the routine, the people, the audit log, the
Settings page, backups, updates, health and lock-outs. It gives the key steps and points to the detailed documents
(`deployment.md`, `backup-restore.md`, `troubleshooting.md`) where the full procedure lives; section 10 is an index from each
operations task to the right section.

## 1. Who is an administrator

An administrator is a member of staff with the `admin` role. That role holds every permission, including the three that no
other role has: `admin.users` (the **Administration** page), `admin.config` (the **Backups**, **Updates** and **Settings** pages) and
the right to read the audit log. The librarian, the circulation clerk and the auditor never see those pages.

| Role | Can do (summary) |
|---|---|
| administrator (`admin`) | everything, including staff and cards, settings, backups, updates, the audit log |
| librarian | catalogue, members, circulation with overrides, tagging, acquisitions, serials, shelves, inventory, gate events, reports, data migration |
| circulation clerk | search, members (read), check out, check in, renew |
| auditor | reports, the audit log (read only), gate events, system health |

Keep at least two people who can act as administrator (the system refuses to remove the last one, section 3). An administrator
also needs shell access to the server for updates and rollback, which are commands and not web pages (section 7).

## 2. Routine checklist

| When | Task | Where |
|---|---|---|
| Daily | Open the dashboard; check that **Gate events** and queued messages are not piling up (**Send queued messages** if the loop is off) | Dashboard, Gate events |
| Daily | Confirm the application answers: `/health` says ok and `/health/ready` shows the schema revision | browser or `curl` |
| Daily | Glance at the audit log for `denied` and `failure` outcomes | Audit log |
| Daily | Copy the newest backup in the backup folder off the server (the application never copies backups away) | file copy |
| Weekly | Review the audit log by person and by action; look for sign-in denials, locked accounts, settings changes | Audit log |
| Weekly | Unlock accounts that staff report as locked, after confirming who they are | Administration |
| Weekly | Run **Compare** on the newest backup against the live data (proves the backup matches) | Backups |
| Monthly | Review the staff list: deactivate leavers, check roles, reissue lost cards | Administration |
| Monthly | Free disk space: delete old backups and old releases by hand (nothing is deleted automatically); `python manage.py prune` removes releases older than the previous one | backup folder, server |
| Monthly | Check the free-space and folder items with `python manage.py doctor` | server |
| Monthly | Read **Settings** and confirm the saved rules still match library policy (changed rows are marked) | Settings |
| Yearly | Rehearse a restore on a copy (or the **Disaster drill** where it is enabled); the proof table must show every table identical | Backups, `backup-restore.md` |
| Yearly | Check the licence date and the signing key held by the site (`manage.py status`, Updates page) | server, Updates |
| Yearly | Review roles against real duties; re-run the site acceptance sign-off for any big change (`uat-signoff.md`) | Administration |

## 3. Staff and cards (the Administration page)

Page `/admin`, permission `admin.users` (administrator only).

- **Staff list.** Shows role, active or deactivated, locked, and each staff card masked (for example `STA…N01`). Full card
  numbers and password hashes are never returned. Search by name or username and filter by role or by status (active,
  deactivated, locked out) to see everyone with a given role. A person is never deleted, only deactivated: the audit log and
  the loan records name who acted.
- **Add a staff member** (username, name, role, optional password) and **issue a card**. A staff card signs in; a patron card only
  identifies a member and can never sign in (enforced in the service and by a database constraint).
- **Deactivate or activate** a person: takes effect at once and ends their open sessions. **Deactivate or reactivate** a card.
  **Unlock** an account locked by wrong passwords. **Change role** to any built-in or custom role.
- **Card rules.** A patron card is checked by the same member-record validation as the circulation desk (the member exists and
  has a name, the card number is 6 to 64 letters and digits and not already used); issuing one switches off the member's old
  card. A staff card cannot be issued to a deactivated person.
- **Roles.** The **Roles** panel lists every role with its permissions and how many staff hold it. The four built-in roles
  (`admin`, `librarian`, `circulation_clerk`, `auditor`) come with the software and are restored at every start, so they are
  read-only here. **Add a custom role** with a name (3 to 32 lowercase letters, digits or `_`), a description and the
  permissions to tick; **Edit** changes a custom role's description and permissions (holders get the change on their next
  click); **Remove** works only when nobody holds the role. Example: a `stock_checker` with `catalogue.read` and
  `inventory.run` only.

Safety rules the page enforces (and the audit log records):

- You cannot deactivate your own account or change your own role, and you cannot take `admin.users` away from your own role.
- You cannot hand out more than you hold: a person with `admin.users` but not, say, `admin.config` cannot give anyone a
  role that has it, create or widen a custom role with it, or change the account, role or cards of someone who holds
  it. The refusal is audited as `authz.denied` with the permissions concerned (D-071).
- The last active administrator cannot be deactivated or demoted, so the library cannot lock itself out.
- A role that someone holds cannot be removed; built-in roles cannot be changed or removed.
- Every change is audited with who, what, and the old and new role; role changes as `admin.role.create`, `admin.role.update`
  (with the permissions added and removed) and `admin.role.delete`. Nothing secret is written to the log.

Limits: no password reset; no new permission types (a custom role combines the permissions the software defines); no deleting
users (deactivate instead, so history stays attributable).
API: `GET /api/admin/people`, `PATCH /api/admin/users/{id}`, `PATCH /api/admin/cards/{id}`, `POST /api/admin/users`, `POST /api/admin/cards`,
`GET /api/admin/roles`, `POST /api/admin/roles`, `PATCH /api/admin/roles/{name}`, `DELETE /api/admin/roles/{name}`.

## 3a. Patrons (the Patrons page)

Page `/members`; anyone with `member.read` can look (desk clerks too). **Blocking** needs `member.write`, **removing and restoring**
needs `member.remove` (librarians and administrators) and **erasing** needs `member.erase` (administrators only).

- **The list.** Every member, a page at a time: name, number, status, e-mail and phone, masked card, items out, fines and the day
  they joined. Search by name, number, e-mail or phone; the buttons *Blocked*, *Items out*, *Owe a fine* and *Removed* filter
  the list and show how many are in each. Removed members only appear under *Removed*.
- **Remove** a member who has left (a reason is optional). Their loans, fines and history are kept; they can no longer borrow,
  their card stops working, waiting reminders are cancelled and they leave the normal lists and search. It is **refused while
  they still have items out or owe a fine**, and the message says what to do first (return the items; record the payment or
  waive the fine).
- **Restore** brings a removed member back as they were (active, or blocked if they were). Their old card stays off: issue a
  new one on the Circulation page.
- **Erase details** (administrators) is a second, irreversible step for a member who is already removed: it replaces the name
  with "Removed member M0007", clears e-mail and phone, deletes their card numbers (so the numbers can be used again) and clears
  the text of their reminder messages. The loan and fine history stays, without a name, so reports still add up. The member
  number must be typed back to confirm, and an erased member cannot be restored: add them again as a new member.
- **What is recorded.** Every removal, restoration and erasure goes to the audit log by member number, with who did it and the
  reason; the log never holds a name, e-mail or phone. **Limits:** entries written before this page existed may still contain a
  name (the log and the circulation history cannot be edited), and with a real ILMS the member also has to be removed there:
  this application never changes the ILMS's records.

## 4. The audit log

Page `/audit`, permission `audit.read` (administrator and auditor).

- Every sign-in, denial and change to library data is one line. The database refuses to edit or delete a line (triggers).
- **Search** by **who**, **action starts with** (for example `circulation.` or `admin.settings.`), **outcome** (success, denied,
  failure) and **text** (record, item or IP). Search text is matched literally: `%` and `_` are not wildcards.
- Newest first, 25 per page, with the total.
- **Export CSV** follows the filters (at most 5,000 lines) and neutralises cells that start with `=`, `+`, `-` or `@` so a
  spreadsheet cannot run them. For a longer period, export in slices by action or person.
- API: `GET /api/audit/events` (paged, with `total`), `GET /api/audit/export.csv`; the older `GET /api/audit` list remains.
- A restore replaces the audit log with the backup's copy up to that moment; the restore itself is logged afterwards.

## 5. The Settings page

Page `/settings`, permission `admin.config` (administrator only). Library rules can be changed in the browser while the
system runs, with no restart and no file editing.

**What you can change.** These fifteen rules, and only these (each has a range; a value outside it is refused with a message):

| Group | Setting | Range | Unit |
|---|---|---|---|
| Loans | Loan period (`circulation.loan_days`) | 1 to 365 | days |
| Loans | Renewals allowed (`circulation.max_renewals`) | 0 to 20 | |
| Loans | Items per member (`circulation.max_loans`) | 1 to 100 | |
| Fines | Block above, the fine limit (`circulation.fine_limit_cents`) | 0 to 1,000,000 | cents |
| Fines | Fine per day (`circulation.fine_per_day_cents`) | 0 to 100,000 | cents |
| Fines | Grace period (`circulation.grace_days`) | 0 to 60 | days |
| Fines | Cap per loan (`circulation.max_fine_per_loan_cents`; 0 = no cap) | 0 to 1,000,000 | cents |
| Reminders | Send loan reminders (`notifications.reminders`) | on or off | |
| Reminders | Remind before due (`notifications.remind_days_before`) | 1 to 30 | days |
| Reminders | Delivery attempts (`notifications.max_attempts`) | 1 to 20 | |
| Gate | Duplicate window (`gate.duplicate_window_s`) | 0 to 3600 | seconds |
| Sign-in | Session length (`security.session_ttl_minutes`; new sign-ins only) | 5 to 10,080 | minutes |
| Sign-in | Failed attempts before lock (`security.max_failed_logins`) | 1 to 20 | |
| Sign-in | Lock time (`security.lockout_minutes`) | 1 to 1440 | minutes |
| Public catalogue | Public catalogue on (`opac.enabled`) | on or off | |

**How it behaves.**

- **Applied at once.** A saved change is live for the next request; the stand-in library system also takes the new loan rules.
- **Audited.** Saving writes one `admin.settings.update` line with the old and new value of each changed setting. A value that is
  already the current value is not recorded. Using **Use file value** writes `admin.settings.reset` with the value it replaced.
- **All or nothing.** Every value is checked first; one bad value saves none of them and the message names the field and the
  allowed range.
- **Stored in the database.** Saved values live in the table `app_settings` (key, value, who, when). They survive restarts and
  they override `config/config.toml`. A row that is no longer valid is ignored at start-up, so a bad row cannot stop the system.
- **Use file value** removes the saved row and the setting goes back to what the file says. A setting that already has the file's
  value cannot be reset (the page says so). Each row shows the file value beside the current one and who changed it and when.
- **Backups include it.** `app_settings` is an ordinary table, so a backup holds the saved rules and a restore brings them back.

**What is read-only there, and where to change it.** The page also lists, without secrets, the library name, environment, address
and whether HTTPS is on, the database file name, the backup folder, the log level, the ILMS (driver, protocol, address), ILMS
retries, net cataloguing, the e-mail, SMS and print drivers, the devices and the message loop interval. Those are set in
`config/config.toml` (or the environment variables) and take effect after a restart, with a person at the server. Secrets (the
secret key, certificates, passwords) never appear on the page. See `deployment.md` (configuration reference) and
`site-addressing.md` (which setting carries which address).

API (same permission): `GET /api/admin/settings`, `PUT /api/admin/settings` with `{"values": {...}}`,
`DELETE /api/admin/settings/{key}`.

## 6. Backups and restore

Page `/backups`, permission `admin.config`; the same actions exist on the command line. Full detail: `backup-restore.md`.

1. **Make a backup** (a button, or `python -m shelfsense.cli backup create --label before-change`). It is verified when made. Backups are also
   taken automatically before every spreadsheet import and every restore. Nothing is deleted automatically.
2. **Copy the backup folder off the machine** regularly (the folder is `[backup] dir`, by default `data/backups`). A nightly job is
   described in `deployment.md` ("Scheduled jobs").
3. **Compare** a backup with the live data, table by table, to see exactly what differs (`sessions` and `audit_events` change with use
   and are not counted as damage).
4. **Restore** only when needed: stop to think, **Compare** first, then **Restore…** and type `RESTORE`. The system saves a safety
   copy (`pre-restore-...`), copies the backup back, and re-proves every table; if anything differs it puts the safety copy back.
5. From the command line, stop the application first and use `backup restore <file> --yes`. Without `--yes` it only explains.
6. For a few seconds everyone else sees "being restored, try again shortly" (503). People may have to sign in again.

A backup from an older version is restored and migrated forward; a backup from a newer version, or a damaged or altered one, is
refused before anything is touched.

## 7. Updates and rollback

Page `/updates` (read and check only) and the command `manage.py` in the install folder. Full detail: `deployment.md`.

1. Put the signed package in `incoming/`; on **Updates** run the check (it changes nothing).
2. Stop the application, then:
   `python <root>\manage.py update <package> --dry-run` (verifies and reports) and then `python <root>\manage.py update <package>`.
3. The update verifies the package, takes and verifies a backup, installs beside the old version, rehearses on a copy of the data,
   migrates the real data, starts once, and only then switches. Any failure before the switch leaves the old version current and the
   data as it was.
4. **Rollback:** `manage.py rollback --dry-run`, then `manage.py rollback --yes`. If the update changed the database layout the
   data returns to the pre-update backup (what was entered since stays in a `pre-restore-...` safety copy); a patch or security
   update only moves the pointer.
5. After a crash or power loss mid-update: `manage.py recover`. Check state any time with `manage.py status`.
6. Updates and rollback are command-line only, because they restart the application.

An update with an expired licence is refused unless it is a security update. Windows install folders must be short (`C:\shelfsense`).

## 8. Monitoring and health

- `/health` answers when the application is up; `/health/ready` checks the database and reports the schema revision.
- `python -m shelfsense.cli doctor` (or `python manage.py doctor`) checks the computer and configuration and prints OK, WARN or FAIL
  for each item; it changes nothing. Run it after any change to the server.
- The structured JSON log (console, `[logging]`) records requests and errors without secrets; the **Audit log** records who did
  what; `manage.py status` shows versions, history and interrupted updates; the **Updates** page shows the same plus the licence.
- There is no built-in alerting. Point a monitoring tool at `/health/ready` (`remaining-work.md`, Operations).
- The gate loop is reported on the Settings page (read-only, "Gate reading"); **Read the gates now** on Gate events shows
  at once whether each gate answers. An offline gate is named there and in the log; the other gates are still read.
- The reminder and message loop is reported on the Settings page (read-only, "Message loop"); queued messages that are not
  being delivered usually mean the loop is off (`auto_dispatch_seconds = 0`).

## 9. Lock-outs and sign-in problems

| Situation | What to do |
|---|---|
| A person is locked after wrong passwords | **Administration**, **Unlock**, or wait for the lock time to end (Settings: Failed attempts before lock, Lock time) |
| "Card or account is disabled" | Activate the person or reactivate the card on **Administration** |
| "Unknown card" | The card was never issued, or the secret key differs from the one the database was created with (keep `config/secret.key` stable) |
| A patron card will not sign in | By design; patron cards only identify members |
| Everyone gets signed out too soon, or never | Check Settings: Session length (applies to new sign-ins only) |
| The only administrator is locked | Wait for the lock time to end; there is no other way in, which is why at least two administrators are kept |
| The sign-in page is unreachable | `/health`, then `troubleshooting.md` |

## 10. Index: operations task to document section

| Task | Where |
|---|---|
| Add or remove staff, issue a card, unlock | this guide, section 3 |
| Search or export the audit log | this guide, section 4 |
| Change loan, fine, reminder, gate or sign-in rules | this guide, section 5 |
| Change a device, driver, ILMS address, TLS file, backup folder | `config/config.toml`; `deployment.md` "Configuration reference"; `site-addressing.md` |
| Make, verify, compare, restore a backup | `backup-restore.md` ("What makes a backup trustworthy", "The restore procedure") |
| A restore says "database stayed busy" or "being restored" | `backup-restore.md`; `troubleshooting.md` |
| Install on a new machine | `deployment.md` "Environment checklist", "Install" |
| Build a signed package | `deployment.md` "Build a package" |
| Update | `deployment.md` "Update" |
| Roll back, recover from an interrupted update | `deployment.md` "Rollback", "If an update goes wrong"; `troubleshooting.md` |
| Run as a service, schedule jobs, set up TLS | `deployment.md` "Running in production" |
| Deliver queued messages and reminders | `deployment.md` "Scheduled jobs"; `reminders.md` |
| Import a spreadsheet and roll it back | `migration.md` |
| Something is not working | `troubleshooting.md` (symptom table and "Where to look") |
| What is not built or not yet proven | `remaining-work.md` |
| Site acceptance sign-off | `uat-signoff.md` |

## 11. Numbers that are too large

Any request with a number SQLite cannot hold (for example `offset=99999999999999999999` or such an id) gets a clean 422 instead of a
server error, for every endpoint (one handler in `app.py`).

## 12. Public terminals and handing over a screen

- **Public terminals** in the reading room use a kiosk host name (`[opac] kiosk_hosts`, `docs/deployment.md`). On that name
  only the public catalogue exists: no staff page, no sign-in, no session. The Settings page lists the kiosk names.
- **A staff screen** is handed to visitors by signing out: the public catalogue is not in the staff menu, it is linked
  from the sign-in page. Opening `/opac` by its address does not sign anyone out; the page then warns that a staff
  session is still open and offers **Sign out and keep the catalogue open** (audited as `auth.logout`, reason
  `public_catalogue`) (DR-17).
- **Hand-run notification actions** (Send queued messages, Run reminders, Retry) are audited as `notification.dispatch`,
  `notification.reminders_run` and `notification.retry`.
