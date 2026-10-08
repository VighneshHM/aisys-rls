# Audit log and administration (FR11)

## Audit log (`/audit`, permission `audit.read`: administrator and auditor)
- Every sign-in, denial and change to library data is one line. The database refuses to edit or delete a line (triggers).
- Filter by **who**, **action starts with** (for example `circulation.`), **outcome** (success, denied, failure) and **text**
  (record, item or IP). Search text is matched literally: `%` and `_` are not wildcards.
- Newest first, 25 per page, with the total. **Export CSV** follows the filters (at most 5,000 lines) and neutralises cells that
  start with `=`, `+`, `-` or `@` so a spreadsheet cannot run them.
- API: `GET /api/audit/events` (paged, with `total`), `GET /api/audit/export.csv`. The older `GET /api/audit` list remains.

## Administration (`/admin`, permission `admin.users`: administrator only)
- **Staff list:** role, active or deactivated, locked, and each staff card shown masked (for example `STA…N01`); full card
  numbers and password hashes are never returned.
- **Add a staff member** (username, name, role, optional password) and **issue a card**: a staff card signs in; a patron card only
  identifies a member and can never sign in (enforced in the service and by a database constraint).
- **Deactivate or activate** a person: takes effect at once and ends their open sessions. **Deactivate or reactivate** a card.
  **Unlock** an account locked by failed password attempts. **Change role** (admin, librarian, circulation_clerk, auditor).
- **Card rules:** a patron card needs an existing member number; a staff card cannot be issued to a deactivated person.
- **Safety rules:** you cannot deactivate your own account or change your own role; the last active administrator cannot be
  deactivated or demoted. Every change is audited (who, what, the old and new role) and never contains secrets.
- API: `GET /api/admin/people`, `PATCH /api/admin/users/{id}`, `PATCH /api/admin/cards/{id}`; creating users and issuing cards
  are the existing `POST /api/admin/users` and `POST /api/admin/cards`.

## Numbers that are too large
Any request with a number SQLite cannot hold (for example `offset=99999999999999999999` or such an id) gets a clean 422 instead of a server error, for every endpoint (one handler in `app.py`).

## Limits
No password reset, no custom roles or per-permission editing, no deleting users (deactivate instead, so history stays attributable).
