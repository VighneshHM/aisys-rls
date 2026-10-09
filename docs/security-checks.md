# Security checks (D6, NFR02)

What was checked, how, and what is left. Each row can be re-run.

## Automated and in the pipeline

| Check | Tool / test | Result |
|---|---|---|
| Static analysis for security defects (Bandit rules) | `ruff check` with the `S` rule set, on every pull request | clean; the few intentional exceptions (subprocess with a fixed argument list, the demo password, constant SQL in migrations) carry a comment saying why |
| Known vulnerabilities in dependencies | `pip-audit` on `requirements.lock` and `requirements-dev.lock` on every pull request | found 7 in the signing library `cryptography` 48.0.0 during this work; fixed by pinning 50.0.2; clean now |
| Secrets in the repository | review of `git ls-files`, `.gitignore` (keys, `.env`, `config/config.toml`, databases, `release-keys/` ignored) | none committed; the only credentials are the synthetic demo cards and the documented demo administrator password |
| Every administrative action recorded (SOP section 10) | the audit log | staff, cards, roles, settings, backups, restores, catalogue corrections and status changes, and the hand-run notification actions (send queued messages, run reminders, retry: added for D-058) each write an audit line with the actor; circulation goes to the append-only history |
| Permissions on every endpoint | `docs/api-reference.md` is generated from the routes and lists the permission each needs; tests call the sensitive ones with the wrong role | a role without the permission gets 403 and an audit line; no endpoint but sign-in, health and the public catalogue is public |
| Passwords and sign-in | `test_foundation`: scrypt hashing, lockout after repeated failures, patron card cannot sign in, disabled person or card refused | passes |
| Session handling | `test_admin`: deactivating a person ends their open sessions at once; tokens are stored hashed | passes |
| ILMS password | `test_sip2`: `AISYS_ILMS_PASSWORD` is the only source (a password in the file is ignored) and it does not appear in a printout of the settings | passes; also not shown by `aisys doctor` |

## Input handling (hostile input tests)

| Input | Where | Result |
|---|---|---|
| SQL-looking and wildcard text (`'; drop table`, `%`, `_`, `\`) | catalogue, members, audit, reports searches | treated as text; `%` and `_` are not wildcards |
| Full-text syntax (`"`, `*`, `OR`, `NEAR(`, `title:x`, `-x`, parentheses, emoji, 500 characters) | catalogue and public search | each is just words; no 500 |
| Path traversal and odd file names (`../../etc/passwd`, spaces, accents, `..`) | backups, update packages, uploads, labels | plain names only, resolved inside their folder; refused otherwise |
| Zip with `../` entries, packages with extra or altered files, unsigned or foreign-signed packages | update tool | refused before anything is changed |
| Spreadsheet formulas (`=cmd|...`, `+`, `-`, `@`) | every CSV export | prefixed so a spreadsheet will not run them |
| HTML in names, titles, notes and search text | pages and public catalogue | shown as text; no element is created |
| Line breaks in a book title | message subjects | removed, so a title cannot add a mail header such as `Bcc:` |
| Addresses with a trailing newline | e-mail, phone, printer names | refused (the rule must match the whole value) |
| Enormous numbers (`offset=99999999999999999999`, ids that large) | every endpoint | a clean 422, never a server error |
| Oversized and malformed bodies, wrong types | all endpoints | 422 with the field and the reason |
| Too many public searches | `/api/opac/search` | 429 after 60 a minute per address |
| Hostile, garbled, truncated, oversized (over 64 KB), wrong-checksum or wrong-sequence SIP2 answers; control and non-ASCII characters | SIP2 decoder (`test_sip2_codec`, fuzz-tested) and client (`test_sip2`) | only `Sip2Error` comes out of decoding; the client asks for a repeat or reports "sent without an answer"; nothing crashes |
| `\|` or control characters in a member or item number (field injection) | SIP2 encoder (`test_sip2_codec`, `test_sip2`) | refused before anything is sent |

## Transport and headers

| Check | Evidence |
|---|---|
| The application serves HTTPS directly when a certificate is configured | `test_security_ops`: a real server on a spare port, a client that trusts the test certificate gets the page over TLS 1.2 or 1.3, one that does not is refused, plain HTTP to the TLS port is refused |
| Cookies are `Secure`, `HttpOnly`, `SameSite=Lax` in production | `test_security_ops` |
| Every response has `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`; HSTS on HTTPS or behind a proxy that says so | `test_security_ops` |
| Camera images are never cached | `Cache-Control: private, no-store` (`test_gate`) |
| Production refuses to start with a short secret key; the installer generates one | `test_config`, `test_lifecycle` |
| SIP2 to the ILMS is **not encrypted** (user, password and patron data in clear text) | Not fixable inside SIP2: use a VPN, stunnel or a TLS-terminating proxy, or an isolated segment (`docs/sip2.md`, `docs/site-addressing.md`). Documented, not tested here |

## Privacy

* All data is synthetic.
* The audit log and the application log never hold a whole card number (`STA…N01`), a password or a token; patron e-mail and phone
  are never written to the audit log (`test_security_ops`, `test_reminders`).
* The public catalogue is not in the staff menu; it is linked from the sign-in page, which shows only once nobody is
  signed in, so the way to it from the workspace goes through signing out (DR-17). Loading the public page itself changes
  nothing, so no link, second tab or API client can sign anyone out (D-055); if a staff session is still open in that
  browser the page says so and offers a sign-out (POST `/handover`: ends the session on the server and in the browser,
  audited as `public_catalogue`, refused when the request's Origin is another site).
  Pages are never cached, so the Back button cannot show a workspace after signing out (`test_opac_session`).
* Public terminals use a kiosk host name (`[opac] kiosk_hosts`): there only the catalogue, its search API, static files and
  the health checks exist, sign-in is impossible, and any cookie or token sent is removed before the request is handled.
* The public catalogue shows no accession number, barcode, tag, member, loan or due date (`test_search_shelves`).
* The staff list shows staff card numbers masked.

## Integrity and supply chain

* Backups are verified when made and again before a restore; the restore proves every table equals the backup and reverts if not
  (`test_backup`). A restore pauses other users so no change is lost.
* Update packages are signed (Ed25519) with a checksum for every file, no unlisted file allowed; a tampered, unsigned,
  foreign-signed, older or wrongly-licensed package is refused before anything is changed (`test_lifecycle`).
* Every package carries an SBOM and a licence inventory (`docs/third-party-licences.md`).
* `requirements.lock` (runtime) and `requirements-dev.lock` (development tools and the build backend) pin every package
  with sha256 hashes; CI installs from them with `--require-hashes`, so a changed or substituted package fails the install.
  `docs/sbom.cdx.json` lists all of them with licences (`scripts/sbom_lock.py`, checked by `test_docs`). `pip-audit`
  scans both files.

## Concurrency and failure

Two administrators deactivating each other at the same moment leave one active administrator; two dispatchers never send a message
twice; two scanners never queue a reminder twice; a restore under four clients' load gave them only 200 or a clean 503
(`test_admin`, `test_reminders`, `test_backup`).

## Known gaps

* **No content-security-policy header**, because the pages use inline scripts; the other headers and the "everything is text"
  rule are the protection today.
* **No penetration test** and no fuzzing beyond the cases above.
* **The credential provider is a mock**; real smart-card challenge-response is a future driver (the interface already carries the
  `proof` field). Assumption A-09.
* **TLS in front of the application is documented, not tested here** (the built-in TLS is tested).
* **The SIP2 link has no encryption and was tested only against the simulator**; the checksum guards against damage, not an attacker.
* **No rate limit on sign-in beyond the account lockout**, and no multi-factor option.
* The SQLite file and the backups are protected by operating-system permissions, not encrypted at rest.
