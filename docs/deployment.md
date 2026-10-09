# Offline install, update and rollback (AC09, FR12, NFR04, NFR06)

AISYS is delivered as one **signed package** (a folder or `.zip`). It contains the application, every dependency, the
tools to install it, a software bill of materials and the licence inventory. Nothing is downloaded at any point after
the package is built, so it works on a computer with no internet.

Try the whole thing in one command (builds two packages, installs, updates, rolls back, with the network deliberately
broken):

```
python scripts/lifecycle_demo.py            # needs the dev extras and internet once, for the build step
python scripts/lifecycle_demo.py --keep     # leaves the installation so you can open the Updates page
```

## The pieces

| Piece | Where | Needs internet? |
|---|---|---|
| Build a package | `scripts/release.py` (build machine, once) | Yes, to fetch the wheels (or `--wheels-from <folder>` for none) |
| Install, update, rollback, recover | `aisys_lifecycle` (Python standard library only) | **No** |
| Signature check | `aisys_lifecycle/ed25519.py` (pure Python, so it runs before anything is installed) | No |
| See the state | **Updates** page and `manage.py status` | No |

## Environment checklist (before installing)

Run `python -m aisys.cli doctor` on the target (or `python manage.py doctor` after installing) and fix every FAIL; it checks the
items below and prints OK, WARN or FAIL for each, changing nothing.

| Item | Needed | Checked by the doctor |
|---|---|---|
| Operating system | Windows 11 (staff PCs) and Windows Server 2022 or later (server); Linux is expected to work and is unverified | reported |
| Python | 3.12 or newer on the server, for the installer only (the application runs in its own environment) | yes |
| SQLite | 3.9 or newer; with FTS5 for full-text search (every normal Python build has it; without it search falls back to plain matching) | yes |
| Disk | at least 500 MB free beside the data folder (a 20,000-item library is about 35 MB; each backup is a full copy; an update needs room for a second environment and a backup) | yes |
| Folders | the install folder (short path on Windows, about 150 characters at most); `data/` and `data/backups/` writable by the account that runs the service | yes |
| Network | Ethernet to the readers, gate, camera, card printer and the ILMS; staff browsers reach the server on the HTTPS port; **no internet needed** | port free: yes |
| Port | 8000 on the loopback address by default; 443 when a proxy or the built-in TLS is used | yes |
| Secret key | 32 or more characters in `AISYS_SECRET_KEY` (the installer writes one to `config/secret.key`) | yes (in production) |
| TLS | a certificate and key (PEM) from the site's own CA or a public one, or a TLS proxy | yes |
| Licence and key | `licence.json` and the vendor's public key supplied with the package | `manage.py status` |
| Backups | a place outside the server to copy `data/backups/` to | not checked |
| Accounts | the service account's rights on the folders; one administrator staff card issued | not checked |

## Configuration reference

Settings come from `config/config.toml` (copy `config/config.sample.toml`); environment variables override the file. A wrong
value stops the start with a plain message. Devices, the ILMS, notification channels and net cataloguing are chosen by *driver
name*; `mock` is built in and a real one is a class registered in code (`docs/architecture.md` section 6).

| Section and key | Default | Meaning |
|---|---|---|
| `[app] name` | AISYS RFID Library | name shown on pages and in messages |
| `[app] environment` | development | `production` turns the simulators off (404), makes cookies Secure and demands a secret key |
| `[app] host`, `port` | 127.0.0.1, 8000 | where the application listens |
| `[database] url` | `sqlite:///data/aisys.db` | the database file |
| `[security] session_ttl_minutes` | 480 | how long a sign-in lasts |
| `[security] max_failed_logins`, `lockout_minutes` | 5, 15 | account lock after repeated wrong passwords |
| `[logging] level`, `format` | INFO, json | `json` or `text` |
| `[backup] dir` | `data/backups` | where backups and safety copies go |
| `[[devices]] id, kind, driver` | one of each kind, `mock` | `staff_reader`, `handheld`, `gate`, `camera`, `card_printer` |
| `[circulation] fine_limit_cents` | 500 | a member whose fines exceed this cannot borrow |
| `[circulation] loan_days`, `max_renewals`, `max_loans` | 14, 2, 5 | loan policy (the ILMS owns it; the mock applies it) |
| `[circulation] fine_per_day_cents`, `grace_days`, `max_fine_per_loan_cents` | 25, 1, 1000 | the fine rule |
| `[ilms] driver` | mock | the ILMS connector |
| `[ilms] protocol` | sip2 | `sip2` or `ncip` (the dialect the mock imitates) |
| `[ilms] endpoint`, `port` | empty, 0 | the ILMS host and its SIP2 / NCIP port, used by a real connector |
| `[ilms] retries`, `backoff_s` | 2, 0.1 | extra attempts and the wait between them |
| `[gate] duplicate_window_s` | 60 | a repeat alarm for the same tag within this many seconds is recorded, not re-sent |
| `[notifications] email, sms, print` | mock | the provider driver per channel |
| `[notifications] max_attempts`, `backoff_s` | 3, 60 | delivery attempts before a message is marked failed; the wait grows with each failure |
| `[notifications] reminders`, `remind_days_before` | true, 1 | patron loan reminders and how many days ahead (1 to 30) |
| `[notifications] auto_dispatch_seconds` | 0 (sample file: 60) | how often the running app queues reminders and delivers messages; 0 = off |
| `[opac] enabled` | true | the public catalogue at `/opac` |
| `[netcat] driver` | mock | where net cataloguing looks books up |
| `[server] tls_certfile`, `tls_keyfile` | empty | serve HTTPS directly (both or neither) |

| Environment variable | Effect |
|---|---|
| `AISYS_CONFIG` | path of the config file |
| `AISYS_ENV` | overrides `environment` |
| `AISYS_DATABASE_URL`, `AISYS_BACKUP_DIR` | override the database and backup folder |
| `AISYS_HOST`, `AISYS_PORT` | override the listening address |
| `AISYS_SECRET_KEY` | the secret (32+ characters; required in production) |
| `AISYS_TLS_CERTFILE`, `AISYS_TLS_KEYFILE` | override the TLS files (an empty value switches TLS off) |
| `AISYS_HOME` | set by the installer: the installation folder (enables the Updates page) |

## What is inside a package

```
aisys-0.2.0/
  manifest.json     name, version, kind (release | patch | security), schema revision, Python needed, SHA-256 of every file
  manifest.sig      Ed25519 signature over manifest.json
  wheels/*.whl      the application and all 22 dependencies
  tools/aisys_lifecycle/   the install/update tools (so a first install needs only Python)
  SBOM.json         CycloneDX software bill of materials
  LICENSES.md       every dependency's licence; copyleft or unknown ones are flagged for review
  config.sample.toml, vendor.pub, install.py
```

A package is refused, before anything is installed or changed, if: the signature does not match a trusted key, a file is
missing, changed or **extra**, the manifest is malformed, a path tries to escape the folder, the version is not newer
than the installed one, it needs a newer Python, the licence is not valid, or the disk is too full.

## Build a package (build machine)

```
python scripts/release.py keygen  --out release-keys                    # once; keep release-keys/ OFF the target machines
python scripts/release.py licence --key release-keys/private.key --customer "Demo Library" --expires 2027-12-31 --out licence.json
python scripts/release.py build   --key release-keys/private.key --notes "what changed"          # dist/aisys-<version>.zip
python scripts/release.py build   --key release-keys/private.key --version 0.1.1 --kind security  # a patch / security update
```

`build` works from a copy of the source, so the repository is never modified. `--version` stamps a different version
into that copy. Give the build `--wheels-from <folder>` to build with no internet.

## Install (target machine, no internet)

```
python install.py --root D:\aisys --licence licence.json --trusted-key vendor.pub [--seed-demo]
```

The vendor's public key should reach you **separately** from the package (`--trusted-key`). For a quick trial,
`--trust-bundle-key` trusts the key inside the package instead (it prints the fingerprint to compare); that proves the
package is intact but not who made it.

It verifies the package, checks the activation (a signed licence file, checked locally), makes a private environment
from the package's own wheels, prepares the database, **starts the application once to prove it runs**, and only then
records the install. If anything fails the half-built release is removed. Then:

```
python D:\aisys\manage.py run
```

The installation folder is plain and visible:

```
releases/<version>/venv   one environment per version; the previous one is kept for rollback
state.json                which release is current and which is previous (changed atomically, last)
history.jsonl             every install / update / rollback / recover
config/                   config.toml, secret.key, licence.json, trusted.pub  (kept across every update)
data/                     the database and its backups  (never inside a release folder)
tools/                    the lifecycle tools, so the commands work even if a release is broken
incoming/                 drop update packages here; the Updates page can check them
```

## Update

Stop the application first (an update restarts it anyway; the tool refuses while it is running).

```
python D:\aisys\manage.py update aisys-0.2.0.zip --dry-run     # verify and say what would happen; changes nothing
python D:\aisys\manage.py update aisys-0.2.0.zip
```

The order is chosen so that the only irreversible moment is the last one:

1. **Verify** signature, every checksum, version, Python, licence, disk space.
2. **Back up** the data with the AC10 backup code and verify the backup.
3. **Install beside** the old version (a new environment from the package's wheels). The old version is untouched.
4. **Rehearse on a copy** of the data: run the new version's migrations on the copy and start the new version on it.
5. **Migrate the real data** and **start the new version once** on it.
6. **Switch** the pointer (`state.json`), then record the history.

If anything fails before step 6: the data is put back from the backup (if step 5 had started), the new release is
deleted, the old version stays current and the history records the failure. A failure in the rehearsal (step 4) never
touches the real data at all.

An update that arrives while the licence is expired is refused unless it is a **security** update (those are always
accepted).

## Rollback (the documented procedure)

```
python D:\aisys\manage.py rollback --dry-run      # what would happen
python D:\aisys\manage.py rollback --yes
```

* The previous version becomes current again. It is started once to prove it works.
* If the update **changed the database layout**, the data is restored from the pre-update backup (verified first, with
  the AC10 restore proof). Anything entered since the update is not in it, but it is kept in a safety copy
  (`pre-restore-...` in the backup folder). Without `--yes` the command explains this and stops.
* If the update **did not change the layout** (a patch or security update), no data is touched at all: only the
  pointer moves, and everything entered since stays.
* Migrations only go forward, which is why the data comes back from a backup instead of a downgrade.

## If an update goes wrong

| Situation | What to do |
|---|---|
| It printed an error and says the old version is still current | Nothing: the old version and its data are as before. Read the message, fix the cause, try again. |
| The computer lost power or the tool was killed mid-update | `python manage.py recover`: puts the data back from the recorded backup if the migration had started, deletes the half-built release, clears the marker. `status` warns about an interrupted update. |
| The automatic data restore itself failed | The message names the backup file. Restore it by hand: `python -m aisys.cli backup restore <file> --yes` using the previous release's python (`releases/<previous>/venv`), see `docs/backup-restore.md`. |
| You want to reclaim disk space | `python manage.py prune` removes releases older than the previous one. |

## Activation (offline)

A licence is a small signed JSON file (customer, issue date, optional expiry). It is checked locally against the
trusted vendor key: no network, no phone-home. An invalid or expired licence stops an **install** and a non-security
**update**; the running application is never switched off. The Updates page shows the licence state.

## Security and licensing (NFR04)

* Ed25519 signatures; the installer pins the key it was installed with and takes later updates only from that key.
* Verification is a short pure-Python implementation of RFC 8032 (so it works before anything is installed), checked
  against the RFC test vector and against signatures made by the `cryptography` library. Signing uses that library on the
  build machine only.
* Dependency versions are pinned in `pyproject.toml` and `requirements.lock`; the wheels shipped are exactly those.
* `SBOM.json` (CycloneDX) and `LICENSES.md` are generated from the shipped wheels and included in every package. The
  current inventory is `docs/third-party-licences.md`: 23 packages, all permissive (MIT, BSD, Apache-2.0, PSF), none
  copyleft. The AISYS code itself is marked `LicenseRef-AISYS-Candidate-Exercise`.
* The private signing key and the secrets are never in a package. `config/secret.key` is generated at install.

## Running in production: service, scheduled jobs, TLS

These are **documented examples, not tested here**: the exercise has no production server. Adapt them to the site.

**Run as a service.** The application is `python <root>/manage.py run`; it holds a lock so an update can tell it is running.
- *Windows:* register it with a service wrapper such as NSSM (`nssm install AISYS C:\Python312\python.exe D:\aisys\manage.py run`),
  or with Task Scheduler ("At startup", "Run whether user is logged on", restart on failure). Stop the service before `manage.py update`.
- *Linux (systemd):* a unit with `ExecStart=/usr/bin/python3 /opt/aisys/manage.py run`, `Restart=on-failure` and a dedicated
  `User=`. `systemctl stop aisys` before an update and `systemctl start aisys` after it.

**Scheduled jobs.** Patron loan reminders and queued notifications are handled by a loop inside the running application when
`[notifications] auto_dispatch_seconds` is set (the sample config sets 60; see `docs/reminders.md`). If you would rather run it as a
separate process, `python -m aisys.cli notify-dispatch --every 60` does the same. Otherwise queued notifications are delivered by `python -m aisys.cli notify-dispatch` (also the **Send queued
messages** button and `POST /api/notifications/dispatch`). To deliver automatically, schedule that command every minute or two
(Task Scheduler or cron), using the installation's own python and settings:
`<root>/releases/<current>/venv/Scripts/python.exe -m aisys.cli --config <root>/config/config.toml notify-dispatch`, with the
environment variables `AISYS_DATABASE_URL` (the database in `<root>/data`) and `AISYS_SECRET_KEY` (from `config/secret.key`) set.
A nightly backup is the same pattern with `backup create --label nightly`. Backups are not deleted automatically.

**TLS and the reverse proxy.** The application speaks plain HTTP on `127.0.0.1` by default; terminate TLS in front of it.
Example (Caddy, with a certificate from the site's own CA or Let's Encrypt):

```
library.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

For nginx use `proxy_pass http://127.0.0.1:8000;` and pass `X-Forwarded-For` and `X-Forwarded-Proto`. In production
(`AISYS_ENV=production`) session cookies are `Secure`, `HttpOnly` and `SameSite=Lax`, and the application refuses to start
without a 32-character `AISYS_SECRET_KEY` (the installer generates one into `config/secret.key`). Uvicorn trusts forwarded
headers only from `127.0.0.1`, so with a proxy on the same machine the audit log shows the real client address. Keep the
reader, gate and handheld devices on the site's wired network, not exposed to the internet.

## Design decisions

| Decision | Why | Cost |
|---|---|---|
| One folder per release, switched by a small pointer file | The old version is untouched until the very end, so a failure costs nothing; rollback is a pointer move | Disk use of one extra environment |
| Rehearse the update on a copy of the data first | Finds a bad migration without risking the real data | An extra minute |
| Reuse the AC10 backup and restore for data safety | One tested, proven mechanism; the restore proves every table matches | Updates need the backup folder to have space |
| Lifecycle tools use only the standard library and live outside the releases | They must work before anything is installed and when a release is broken | A small separate package to maintain |
| Pure-Python signature verification | The installer may run on a machine with nothing installed | Slow (10 ms), which does not matter for one small file |
| Signed manifest with a hash for every file, and no extra files allowed | Detects tampering, truncation and smuggled files | Rebuilding a package changes its signature |
| Update refuses while the app runs, rolls back by restoring the backup | Predictable; avoids half-applied changes under live traffic | A short planned outage |
| Applying and rolling back are command-line only; the web page only reads and checks | They restart the app; a web request cannot safely restart its own server | Needs shell access to the server |

## Known limits

* Windows and Linux are supported by the same `python install.py` / `manage.py`; it is not a Windows service or
  systemd unit (run `manage.py run` under your service manager). Only Windows has been exercised here.
* Packages contain wheels for the platform they were built on; build on the target's platform and Python version.
* SQLite databases only (the backup code is SQLite-only).
* On Windows the installation folder must be short (about 150 characters including `releases\<version>\venv`; use something like `C:\aisys`). The installer checks this first and says so, because Windows stops at 260 characters per path.
* A rollback across a layout change returns the data to the pre-update moment (by design).
* A first install trusts the key you give it; there is no certificate authority or key revocation. Replace the trusted
  key by reinstalling.
* The licence has no hardware binding.
