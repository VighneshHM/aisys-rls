"""Command line: `aisys migrate | seed-demo | run | version`."""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from aisys import __version__, audit, migrate
from aisys.auth import security
from aisys.auth.permissions import sync_rbac
from aisys.config import Settings, load_settings
from aisys.db import make_engine, make_sessionmaker
from aisys.models import Card, FineEntry, Member, Role, User

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


def seed_demo(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    with make_sessionmaker(engine)() as db:
        sync_rbac(db)
        for username, name, role_name, card_uid in DEMO_USERS:
            if db.scalar(select(User).where(User.username == username)):
                continue
            role = db.scalar(select(Role).where(Role.name == role_name))
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
        audit.record(db, actor="system", action="seed.demo", detail={"users": len(DEMO_USERS)})
        db.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aisys")
    parser.add_argument("--config", help="path to config TOML")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("seed-demo", help="load synthetic demo users and cards")
    sub.add_parser("run", help="start the web application")
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
    elif args.cmd == "run":
        import uvicorn

        from aisys.app import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
