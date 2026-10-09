"""Command line: `aisys migrate | seed-demo | run | version`."""

from __future__ import annotations

import argparse
import sys
import threading

from sqlalchemy import select

from aisys import __version__, audit, migrate
from aisys.auth import security
from aisys.auth.permissions import sync_rbac
from aisys.config import Settings, load_settings
from aisys.db import make_engine, make_sessionmaker
from aisys.models import Card, FineEntry, Member, NotificationPref, Role, User

# SYNTHETIC demo identities only (SOP stage 1: all data and credentials are synthetic).
DEMO_PASSWORD = "Demo-Admin-Pass-1"  # noqa: S105 - synthetic, documented demo credential
DEMO_USERS = [
    ("admin", "Ada Admin", "admin", "STAFFADMIN01"),
    ("librarian", "Leo Librarian", "librarian", "STAFFLIB0001"),
    ("clerk", "Cleo Clerk", "circulation_clerk", "STAFFCLERK01"),
    ("auditor", "Ari Auditor", "auditor", "STAFFAUDIT01"),
]
# (card uid, member no, name, status, block reason, fines in cents): one of each situation circulation must handle
DEMO_MEMBERS = [
    ("PATRON000001", "M0001", "Priya Nair", "active", "", 0),
    ("PATRON000002", "M0002", "Tom Becker", "active", "", 0),
    ("PATRON000003", "M0003", "Lena Okafor", "blocked", "Overdue items not returned", 0),
    ("PATRON000004", "M0004", "Sam Ortiz", "active", "", 1250),  # fines over the 5.00 limit
    ("PATRON000005", "M0005", "Ana Silva", "active", "", 300),  # owes something, still under the limit
    ("PATRON000006", "M0006", "Kim Park", "active", "", 0),
]


# staff who get gate alerts in the demo: username -> (channel, address). SYNTHETIC .example addresses only.
# synthetic contact details for the demo patrons (.example addresses and reserved test numbers reach no real person)
DEMO_CONTACTS = {
    "M0001": ("priya.nair@patron.example", "+15550100001"),
    "M0002": ("tom.becker@patron.example", ""),
    "M0005": ("", "+15550100005"),
}
DEMO_ALERTS = {
    "admin": [("email", "security@library.example")],
    "librarian": [("email", "head-librarian@library.example"), ("print", "front-desk")],
}


def seed_demo(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    with make_sessionmaker(engine)() as db:
        sync_rbac(db)
        for username, name, role_name, card_uid in DEMO_USERS:
            if db.scalar(select(User).where(User.username == username)):
                continue
            role = db.scalar(select(Role).where(Role.name == role_name))
            assert role is not None  # sync_rbac has just created every role
            user = User(
                username=username,
                display_name=name,
                role_id=role.id,
                password_hash=security.hash_password(DEMO_PASSWORD) if username == "admin" else None,
            )
            db.add(user)
            db.flush()
            db.add(Card(uid=card_uid, kind="staff", user_id=user.id))
        for uid, member_no, name, status, reason, fines in DEMO_MEMBERS:
            if not db.scalar(select(Member).where(Member.member_no == member_no)):
                new = Member(member_no=member_no, name=name, status=status, block_reason=reason, fines_cents=fines)
                db.add(new)
                db.flush()
                if fines:  # every balance is explained by the ledger
                    db.add(
                        FineEntry(
                            member_id=new.id, kind="opening", amount_cents=fines, note="Opening balance", actor="system"
                        )
                    )
            if not db.scalar(select(Card).where(Card.uid == uid)):
                db.add(Card(uid=uid, kind="patron", member_ref=member_no))
        db.flush()
        for member_no, (email, phone) in DEMO_CONTACTS.items():
            owner = db.scalar(select(Member).where(Member.member_no == member_no))
            if owner and not owner.email and not owner.phone:  # never overwrite what staff entered
                owner.email, owner.phone = email, phone
        for username, channels in DEMO_ALERTS.items():
            alert_user = db.scalar(select(User).where(User.username == username))
            if alert_user is None:
                continue
            for channel, address in channels:
                have = db.scalar(
                    select(NotificationPref).where(
                        NotificationPref.user_id == alert_user.id, NotificationPref.channel == channel
                    )
                )
                if not have:
                    db.add(NotificationPref(user_id=alert_user.id, channel=channel, address=address))
        audit.record(db, actor="system", action="seed.demo", detail={"users": len(DEMO_USERS)})
        db.commit()


def _backup_command(args, settings: Settings) -> int:
    from pathlib import Path

    from aisys import backup

    try:
        if args.bk_cmd == "create":
            info = backup.create_backup(settings.database_url, settings.backup_dir, args.label)
            print(f"backup created: {info.path}  ({info.size / 1024:.0f} KB, sha256 {info.sha256[:16]}...)")
            return 0
        if args.bk_cmd == "list":
            rows = backup.list_backups(settings.backup_dir)
            for b in rows:
                print(
                    f"{b['name']}  {b['size'] / 1024:>8.0f} KB  schema {b['revision'] or '?'}  {b['created_at'][:19]}"
                )
            print(f"{len(rows)} backup(s) in {settings.backup_dir}")
            return 0
        path = Path(args.file)
        if not path.is_file():
            path = Path(settings.backup_dir) / args.file
        if args.bk_cmd == "verify":
            v = backup.verify_backup(path)
            print("OK: healthy and restorable" if v["ok"] else "NOT OK: " + "; ".join(v["problems"]))
            return 0 if v["ok"] else 1
        live = backup.sqlite_file(settings.database_url)
        if args.bk_cmd == "compare":
            c = backup.compare(live, path)
            for t in c["tables"]:
                note = "activity" if t["activity"] and not t["match"] else ("same" if t["match"] else "DIFFERENT")
                print(f"  {t['table']:<22} live {t['live_rows']!s:>7}  backup {t['backup_rows']!s:>7}  {note}")
            if c["data_intact"]:
                print("The data is identical to the backup.")
            elif c["library_data_intact"]:
                print(
                    "Library data is identical to the backup (only the import log differs: "
                    + ", ".join(c["differing"])
                    + ")."
                )
            else:
                print("DIFFERS in: " + ", ".join(c["differing"]))
            return 0 if c["library_data_intact"] else 1
        if args.bk_cmd == "restore":
            if not args.yes:
                print(
                    f"This will replace ALL current data with {path.name}. "
                    "Stop the application first, then repeat with --yes."
                )
                return 2
            result = backup.restore_backup(settings.database_url, path, settings.backup_dir)
            if result["needs_upgrade"]:
                migrate.upgrade(settings.database_url)
            with make_sessionmaker(make_engine(settings.database_url))() as db:
                audit.record(
                    db,
                    actor="cli",
                    action="backup.restore",
                    entity="backup",
                    entity_id=path.name,
                    detail={"safety_backup": result["safety_backup"], "tables_match": True},
                )
                db.commit()
            n = len(result["proof"]["tables"])
            print(
                f"restored {path.name}: all {n} tables match the backup. "
                f"Safety copy of the previous data: {result['safety_backup']}"
            )
            return 0
    except backup.BackupError as exc:
        print("error:", exc)
        return 1
    return 0


def serve_options(settings: Settings) -> dict:
    """Arguments for uvicorn: where to listen and, when a certificate is configured, HTTPS. Fails early and plainly."""
    from pathlib import Path

    options = {"host": settings.host, "port": settings.port, "log_config": None}
    cert, key = settings.tls_certfile, settings.tls_keyfile
    if bool(cert) != bool(key):
        raise SystemExit("error: set both tls_certfile and tls_keyfile (or neither)")
    if cert:
        for label, path in (("certificate", cert), ("private key", key)):
            if not Path(path).is_file():
                raise SystemExit(f"error: the TLS {label} file was not found: {path}")
        options.update(ssl_certfile=cert, ssl_keyfile=key)
    return options


def _notify_command(args, settings: Settings) -> int:
    """One pass (queue due patron reminders, deliver what is queued), or with --every keep doing it until stopped."""
    import time

    from aisys.notifications import runner
    from aisys.notifications.providers import build_providers

    factory = make_sessionmaker(make_engine(settings.database_url))
    providers = build_providers(settings.notifications)

    def one_pass() -> dict:
        result = runner.run_once(factory, providers, settings.notifications, library_name=settings.app_name)
        return result

    if not args.every:
        print("notifications:", one_pass())
        return 0
    if args.every < 1:
        print("error: --every must be at least 1 second")
        return 2
    print(f"checking for reminders and delivering messages every {args.every:g} s (Ctrl+C to stop)")
    loop = runner.Loop(one_pass, args.every)
    loop.start()
    last = None
    try:
        while True:
            time.sleep(1)
            if loop.last is not None and loop.last is not last:
                last = loop.last
                busy = last["reminders"]["queued"] or last["delivery"]["tried"]
                if busy:
                    print(time.strftime("%H:%M:%S"), last)
    except KeyboardInterrupt:
        pass
    finally:
        loop.stop()
    print("stopped")
    return 0


def _sip2_sim(args: argparse.Namespace, settings: Settings) -> int:
    """Run the SIP2 simulator until interrupted. It is a stand-in for an ILMS, never for production."""
    from aisys.circulation.sip2.server import Sip2Server

    users = {args.user: args.password} if args.user else None
    server = Sip2Server(args.host, args.port, config=settings.circulation, users=users, institution="LIBRARY").start()
    print(f"SIP2 simulator listening on {server.host}:{server.port} (Ctrl+C to stop)", flush=True)
    print("login:", f"user {args.user!r}" if args.user else "none needed", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aisys")
    parser.add_argument("--config", help="path to config TOML")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("seed-demo", help="load synthetic demo users and cards")
    bk = sub.add_parser("backup", help="create, list, verify, compare or restore backups")
    bsub = bk.add_subparsers(dest="bk_cmd", required=True)
    bc = bsub.add_parser("create", help="make a verified backup now")
    bc.add_argument("--label", default="manual")
    bsub.add_parser("list", help="list the backups")
    for name, text in (
        ("verify", "check a backup is healthy"),
        ("compare", "compare the live data with a backup"),
        ("restore", "replace ALL current data with a backup (stop the app first)"),
    ):
        bp = bsub.add_parser(name, help=text)
        bp.add_argument("file", help="a backup file name (in the backup folder) or a path")
        if name == "restore":
            bp.add_argument("--yes", action="store_true", help="really do it")
    sub.add_parser("run", help="start the web application")
    nd = sub.add_parser("notify-dispatch", help="queue due patron reminders and deliver queued notifications")
    nd.add_argument("--every", type=float, default=0, metavar="SECONDS", help="keep running, repeating every N seconds")
    sim = sub.add_parser("sip2-sim", help="run a SIP2 test server that stands in for an ILMS (simulator)")
    sim.add_argument("--host", default="127.0.0.1")
    sim.add_argument("--port", type=int, default=6001)
    sim.add_argument("--user", default="", help="require this login user (with --password)")
    sim.add_argument("--password", default="", help="the login password; omit both for a server with no login")
    sub.add_parser("doctor", help="check this computer and configuration (environment checklist)")
    sub.add_parser("version", help="print version")
    args = parser.parse_args(argv)

    if args.cmd == "version":
        print(__version__)
        return 0
    settings = load_settings(args.config)
    if args.cmd == "migrate":
        migrate.upgrade(settings.database_url)
        print("database is at head:", migrate.head_revision())
    elif args.cmd == "seed-demo":
        migrate.upgrade(settings.database_url)
        seed_demo(settings)
        print(f"seeded demo data (staff cards STAFFADMIN01 etc.; admin password: {DEMO_PASSWORD})")
    elif args.cmd == "notify-dispatch":
        return _notify_command(args, settings)
    elif args.cmd == "sip2-sim":
        return _sip2_sim(args, settings)
    elif args.cmd == "doctor":
        from aisys import doctor

        results = doctor.run(settings)
        print(doctor.render(results))
        return 1 if any(status == "FAIL" for status, _, _ in results) else 0
    elif args.cmd == "backup":
        return _backup_command(args, settings)
    elif args.cmd == "run":
        import uvicorn

        from aisys.app import create_app

        uvicorn.run(create_app(settings), **serve_options(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
