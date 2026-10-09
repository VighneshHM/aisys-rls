# Design decisions and design-review record (D2, Stage 1)

One entry per feature that was designed before it was built. How the review worked, stated plainly: the design was proposed by the
AI assistant (Claude) in the working chat, with alternatives; **the project owner (VighneshHM) reviewed it and approved or changed it
in that chat**, and only then was it built. There was no second human reviewer and no meeting. The chat itself is not stored in the
repository, so the **Date** column is the date of the first commit that implements the decision (from `git log`), which is the
latest the decision can have been made; the discussion happened on or just before that day. The summary table of the same
decisions is in `docs/architecture.md` section 9; this file adds the reviewer, date and consequence.

Reviewer for every entry: **VighneshHM (project owner), approved in chat.**

## DR-01 Connector and mocks architecture

* **Date:** 2026-10-05 (base setup, `1ccefb4`); extended 2026-10-06 (circulation, PR #8).
* **Feature:** every external system (ILMS, readers, handheld, gate, camera, card printer, e-mail/SMS/print, credentials,
  outside catalogue) sits behind a `typing.Protocol` with a mock and a config-selected driver (registry).
* **Proposed:** the add-on never imports a vendor SDK or a mock directly; mocks are chosen by configuration.
* **Alternatives considered:** hard-wired mocks (quicker, but a real device would mean rewriting callers); building a full ILMS
  (the SOP asks to integrate with an existing one, A-02).
* **Decision:** Protocol + mock + registry for everything external.
* **Consequence:** a real device or ILMS is one class plus a config line. The mocks prove the interface, not any vendor's behaviour
  (risk R-17). FR01 acquisition and serials are reference-level only.

## DR-02 SQLite with Alembic

* **Date:** 2026-10-05 (`1ccefb4`).
* **Proposed:** SQLite in WAL mode through SQLAlchemy 2, schema created only by versioned Alembic migrations shipped in the package.
* **Alternatives considered:** PostgreSQL (needs a server install on an isolated site); no migration tool (cannot support the
  offline update with data migration, AC09).
* **Decision:** SQLite + SQLAlchemy + Alembic. PostgreSQL stays a URL change in principle.
* **Consequence:** zero-install database and FTS5 built in; one writer at a time (risk R-19); database rules (unique indexes,
  check constraints, append-only triggers) are SQLite-specific. Migrations now run `0001` to `0011`.

## DR-03 Device first, then database, with undo

* **Date:** 2026-10-06 (RFID tagging, `d0c3550`, PR #7).
* **Proposed:** when tagging an item, write the tag on the reader before saving the database row, and undo the device write if the
  save fails.
* **Alternatives considered:** database first, then device (a failed device write would leave a record that says an item is tagged
  when it is not); a two-phase protocol (a reader cannot join a database transaction).
* **Decision:** device first, database second, compensating undo (A-14).
* **Consequence:** the database never claims a tag that was not written. A crash between the two steps can leave a written tag with no
  record; the tag can be re-read and re-tagged. Failure injection is available on the mock reader.

## DR-04 Staff card is a login; a patron card never logs in

* **Date:** 2026-10-05 (foundation and AC07); asked as clarification C-04.
* **Proposed:** a staff card, presented to a credential provider, creates a session; a patron card only identifies a member inside a
  transaction.
* **Alternatives considered:** one card type usable for both (a borrowed or lost patron card would become a staff login); checking
  the rule only in code.
* **Decision:** the rule is enforced in `auth/service.py` **and** by the database check constraint `ck_cards_owner` (a card is staff or
  patron, never both) (A-04).
* **Consequence:** a patron card is refused at login and audited. The mock reader presents only a UID, so a cloned card would pass
  (risk R-07); a challenge-response driver is future work.

## DR-05 No SIP2 or NCIP wire protocol (FR03)

**Superseded for SIP2 by DR-12 (2026-10-09). NCIP is not built, by the decision in DR-13.** The entry is kept as it was written.

* **Date:** 2026-10-06 (circulation, PR #8); recorded as A-15 and C-01.
* **Proposed:** build the connector boundary, a mock that words replies as SIP2 or NCIP, retries and tests; do not implement a wire
  protocol client.
* **Alternatives considered:** a SIP2 client written from the spec (large, and untestable without a real ILMS or the vendor's
  variant); an NCIP 2.0 XML client (same problem).
* **Decision:** deliberately left out, following SOP section 3 (production systems out of scope). FR03 stays `partial` in
  `docs/requirements.csv`.
* **Consequence:** no protocol conformance is claimed anywhere (A-11). Connecting a real ILMS is one driver class. Risk R-16.

## DR-06 Offline signed packages with a pure-Python Ed25519 verifier

* **Date:** 2026-10-08 (`63721fb`, PR #16).
* **Proposed:** install and update from one signed ZIP with a manifest, a checksum per file and an Ed25519 signature, verified before
  anything is installed; the installer pins the key it was installed with.
* **Alternatives considered:** checksums only (no proof of origin); verifying with the `cryptography` library (it is not installed
  yet at first install, so trust would be checked after the code ran).
* **Decision:** a small pure-Python Ed25519 verifier in `aisys_lifecycle/ed25519.py`, tested against the RFC 8032 vectors and against a
  real library; `cryptography` is used only in tests and for signing.
* **Consequence:** the installer needs only Python. Verification takes about 10 ms and the verifier is my own code (risk R-10). There is no
  certificate authority and no key revocation.

## DR-07 Patron reminders: interpretation A

* **Date:** 2026-10-08 (`86a1f26`, PR #19); recorded as A-25 and C-02.
* **Proposed:** the requirement "remind patrons" is ambiguous. The assistant offered readings; the owner chose **A**: remind once the
  day before a loan is due and once on the due day, by e-mail and/or SMS.
* **Alternatives considered:** reminders only on the due day; repeated overdue notices (not asked for).
* **Decision:** A. One queued message per loan, kind, channel and due date (unique index), delivered by a loop inside the app; a returned
  or renewed loan's reminder is cancelled at delivery. Overdue notices are not part of it.
* **Consequence:** if the application is down for a whole day, that day's reminder is not sent later (`docs/remaining-work.md`).
  Defects D-029 to D-033 were found in this feature.

## DR-08 Full-text search with FTS5, used only when it has hits

* **Date:** 2026-10-08 (A-26, `ad8a3b6`); refined 2026-10-09 after defect D-028.
* **Proposed:** an FTS5 index with one row per item, kept current by triggers; "contains" matching as a fallback.
* **Alternatives considered:** an external search engine (Whoosh, Elasticsearch: another service on an isolated site); `LIKE` only
  (no ranking, slow).
* **Decision:** FTS5 by triggers. After a performance run showed search taking 1.3 to 3.3 s at 20,000 items because the full-text match
  and the substring match were combined in one query, the substring search runs **only when the index has no hits** (about 120 ms).
* **Consequence:** the index lives in the same file and is derived data (rebuilt by migration, excluded from the integrity fingerprint).
  A partial word that the index does not match is found only when nothing else matches.

## DR-09 Maintenance mode during restore

* **Date:** 2026-10-08 (`9143fad`, PR #14).
* **Proposed:** while a restore runs, every other request gets a clean 503; two restores cannot overlap; a locked database ends the
  attempt after 20 s with "nothing was changed".
* **Alternatives considered:** do nothing and rely on SQLite locking (a second user's change could be lost or half applied, found as D-012);
  require the service to be stopped first (less usable for a small library).
* **Decision:** maintenance mode for the few seconds of a restore, a safety copy first, typed confirmation RESTORE.
* **Consequence:** a brief outage on restore; a restore replaces all data including the audit log up to the backup's moment (A-21).

## DR-10 Notifications are queued, then delivered

* **Date:** 2026-10-07 (`9d6c0bd`, PR #12).
* **Proposed:** an event (gate alarm, later a reminder) writes a row to a notification queue; a separate step (button, API, CLI or the
  in-app loop) delivers it with retries and back-off.
* **Alternatives considered:** send inline when the event happens (a mail outage would block or lose the alarm).
* **Decision:** queue first, deliver second; each message is claimed atomically before sending (after D-029 showed double delivery with two
  dispatchers).
* **Consequence:** an alarm is never lost to a provider outage; delivery is slightly later than the event; failures are visible in the
  queue.

## DR-11 Settings page: rules in the browser, secrets and devices in TOML

* **Date:** 2026-10-09 (`39d38fe`, merged in PR #22).
* **Proposed:** library rules that are safe to change while running (loan days, renewals, fine rates and limit, reminder days, gate
  duplicate window, sign-in limits, public catalogue switch) are editable by an administrator on a Settings page and stored in a new
  `app_settings` table (migration `0011`). A stored value overrides the TOML file; removing it returns to the file. Database, devices,
  drivers, certificates and secrets stay in the TOML file and environment.
* **Alternatives considered:** edit everything in the browser (a web page would hold secrets and device addresses, and most of those
  need a restart and a person at the server); keep everything in TOML (every rule change needs file access to the server).
* **Decision:** the split above. Each setting has a range check.
* **Consequence:** rule changes take effect without a restart and are attributable (`updated_by`, `updated_at`). Two places now hold
  configuration; the table wins over the file for the listed keys. Tests are in `tests/test_settings.py`.

## DR-12 SIP2 wire protocol built after all

* **Date:** 2026-10-09 (`d777380`, merged in PR #22); recorded as C-09 and A-33 to A-40.
  Supersedes DR-05 for SIP2.
* **Context:** DR-05 left the wire protocol out because it seemed untestable without a real ILMS, which kept FR03 `partial`. After the
  review the owner asked for it to be added.
* **Proposed:** a SIP2 (3M 2.00) client behind the existing connector boundary, registered as driver `sip2`: a pure codec, a TCP
  session with login, resend handling and a checksum, and an adapter that maps ILMS answers to the application's neutral codes. Because
  there is no real ILMS, a SIP2 simulator written from the specification is built with it, so the client can be tested over a real socket,
  including lost, dropped and damaged answers. SIP2 has no transaction id, so a request without an answer is verified against the ILMS
  before it is repeated.
* **Alternatives considered:** NCIP 2.0 instead of, or as well as, SIP2 (an XML protocol, a larger piece of work; deferred, and it would be
  another class behind the same boundary); keeping the exclusion and the mock only (FR03 stays partial); resending blindly after a lost
  answer (risks lending or renewing twice).
* **Decision:** SIP2 only, in the same branch. `[ilms] driver = "sip2"` selects it; `mock` stays the default. The password is read only from
  `AISYS_ILMS_PASSWORD`.
* **Consequence:** FR03 is `done`, with limits stated plainly: tested only against the simulator, **not certified and not tested against
  any vendor ILMS**; a subset of SIP2; NCIP 2.0 not implemented; SIP2 is unencrypted so a tunnel is needed; vendor wording may need keyword
  changes in `adapter.py`. Risk R-16 was reworded accordingly. Details: `docs/sip2.md`.

## DR-13 NCIP 2.0 is not implemented; SIP2 is the protocol

* **Date:** 2026-10-09 (branch `fix/sop-gaps`); recorded as C-10 and A-41.
* **Context:** SOP FR03 reads "Support NCIP 2.0 and an SIP2 adapter boundary", and AC03 asks for "a mocked NCIP or SIP2 flow". After
  DR-12 the SIP2 client was real, and the final coverage review asked whether an NCIP 2.0 adapter was still needed.
* **Proposed:** treat the protocol requirement as met by SIP2; keep NCIP as a documented extension point only.
* **Alternatives considered:** an NCIP 2.0 XML adapter with its own simulator (another protocol of the same size as SIP2, with no
  real NCIP responder to test against either); an NCIP message model with no transport (looks like support but is not).
* **Decision:** the project owner decided that **SIP2 satisfies the "NCIP 2.0 / SIP2" protocol requirement for this exercise and NCIP
  2.0 is not to be built.** AC03 is met through SIP2 ("NCIP or SIP2"). Nothing in the repository claims NCIP support (SOP section 3).
* **Consequence:** an NCIP 2.0 connector stays future work behind the same `CirculationConnector` boundary (one class plus a
  `register_driver` line, `docs/remaining-work.md`). The mock ILMS can still word its replies NCIP-style for demonstrations; that is
  wording, not protocol. A site whose ILMS speaks only NCIP needs that connector first (risk R-16).

## DR-14 Custom roles next to the four built-in ones

* **Date:** 2026-10-09 (branch `fix/sop-gaps`).
* **Context:** FR11 asks for "user and role management". Roles could be assigned to people but not created or changed.
* **Proposed:** an administrator (`admin.users`) can create a role with any set of the catalogued permissions, change its
  description and permissions, and remove it when nobody holds it. The four built-in roles (admin, librarian,
  circulation_clerk, auditor) stay read-only, because `sync_rbac` restores them from the code at every start.
* **Alternatives considered:** make the built-in roles editable too (an edit would be silently undone at the next start, or the
  release could never fix a role); a new permission `admin.roles` (one more thing to grant; the people who manage staff are the
  people who should decide their rights).
* **Decision:** custom roles only, with two guards: an administrator cannot remove `admin.users` from their own role, and a role
  in use cannot be removed. Every create, change and removal is audited (`admin.role.*`), with the permissions added and removed.
* **Consequence:** a site can define, for example, a stock checker with only `catalogue.read` and `inventory.run`. Rights change
  on the holder's next request. No schema change was needed: roles and permissions were already tables.

## DR-15 One member-record check for every way a patron card is linked

* **Date:** 2026-10-09 (branch `fix/sop-gaps`).
* **Context:** FR05 asks to validate "title or member records before tagging or re-tagging". Items had one validation
  (`validate_tagging`); patron cards were checked in `circulation/members.py`, and the Administration page's card form had its own,
  weaker checks (it did not switch off a member's previous card).
* **Decision:** `rfid/tagging.py` has `validate_member_tagging`, the member-side twin of `validate_tagging`: it reports every
  problem at once (no such member, no name, bad or used card number) and the warnings (the old card will be switched off; the
  member is blocked). The circulation desk, the Administration page and a new dry-run endpoint
  (`POST /api/circulation/members/{no}/card/validate`) all use it.
* **Consequence:** issuing a patron card from the Administration page now also switches off the member's old card, as the desk
  always did.

## DR-16 Runtime and development locks; a real network block for the offline rehearsal

* **Date:** 2026-10-09 (branch `fix/sop-gaps`).
* **Lock files:** `requirements.lock` now holds only what the application needs to run (it is also what `scripts/release.py`
  builds the shipped wheels from); `requirements-dev.lock` holds the test, lint, type-check and signing tools and the build backend
  (`setuptools`, pinned, so the editable install builds without fetching an unpinned backend). Both carry sha256 hashes and CI
  installs both with `--require-hashes`.
* **Offline rehearsal:** `scripts/lifecycle_demo.py` used to set a dead proxy, which only stops programs that honour proxy
  settings. It now loads a guard into every Python process of the target-machine steps (passed on to child processes even when the
  installer cleans their environment) that refuses any connection or name lookup that is not to the machine itself and logs it;
  the run fails if the log is not empty. A probe at the start proves the guard is active.
* **Alternative considered:** a firewall rule on the CI machine (it would also cut the runner off from GitHub and needs
  administrator rights on a developer's PC).
