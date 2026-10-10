"""The full demo library: about a hundred days of synthetic library life, so every menu has something in it.

`python -m aisys.cli seed-demo` loads it into an empty catalogue (`--minimal` loads only the staff and patron cards the
automated tests use). Nothing here is written straight into tables: every step goes through the application's own
HTTP API, in this process, signed in as the member of staff who would do it, so the records, audit lines, fines and
notifications are exactly what the real screens produce. History is replayed at the moments it is meant to have
happened (`timeutil.clock_at`), oldest first, so loans taken and returned weeks ago carry those dates.

What it creates (all synthetic: names, cards, addresses on .example domains, reserved test phone numbers):

- staff: 1 administrator, 2 librarians, 3 circulation clerks, 1 auditor (the sign-in page still offers one of each);
- the 200-row sample spreadsheet imported (191 books), every imported book tagged; about 70 more books catalogued by
  hand, received from orders or booked in from serials, 15 of them left untagged;
- 20 patrons: active, blocked, owing fines, over the fine limit, removed, without a card, with a replaced card, with
  past loans returned (some late, with fines paid or waived) and current loans (some overdue, some renewed);
- shelves, inventory sweeps (one closed, one open), gate passages and alarms with snapshots, purchase orders in every
  state, serial subscriptions with received, late, claimed and missing issues, label and card print jobs, reminders
  and alerts sent, settings changed, backups made, and the audit trail of all of it.

The Updates page only has a history in a copy installed from a package (`install.py`), so it is not seeded here.
"""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func, select

from aisys.config import Settings
from aisys.devtools import sample_data
from aisys.gate.config import GateConfig
from aisys.models import Item, Loan, Member, RfidTag
from aisys.notifications.providers import NotificationConfig
from aisys.rfid.registry import DEFAULT_DEVICES
from aisys.timeutil import clock_at

# the extra staff (the sign-in page shows only the first of each role, from cli.DEMO_USERS)
EXTRA_STAFF = [
    # username, display name, role, cards in the order they were issued (the first one is reported lost later)
    ("noor", "Noor Haddad", "librarian", ["STAFFLIB0002"]),
    ("ravi", "Ravi Menon", "circulation_clerk", ["STAFFCLERK02"]),
    ("jonas", "Jonas Weber", "circulation_clerk", ["STAFFCLERK3X", "STAFFCLERK03"]),
]
STAFF_CARDS = {
    "admin": "STAFFADMIN01",
    "leo": "STAFFLIB0001",
    "cleo": "STAFFCLERK01",
    "ari": "STAFFAUDIT01",
    "noor": "STAFFLIB0002",
    "ravi": "STAFFCLERK02",
}
JONAS_CARD_LOST_DAY = -28

# the 14 patrons added to the 6 of cli.DEMO_MEMBERS: name, card uid ("" = none yet), e-mail, phone
EXTRA_PATRONS = [
    ("Meera Iyer", "PATRON000007", "meera.iyer@patron.example", "+15550100007"),
    ("Daniel Brooks", "PATRON000008", "daniel.brooks@patron.example", ""),
    ("Hana Sato", "PATRON000009", "", "+15550100009"),
    ("Omar Farouk", "PATRON000010", "omar.farouk@patron.example", "+15550100010"),
    ("Grace Mensah", "PATRON000011", "grace.mensah@patron.example", ""),
    ("Lucas Moreau", "PATRON000012", "", ""),
    ("Sofia Rossi", "PATRON000013", "sofia.rossi@patron.example", "+15550100013"),
    ("Arjun Rao", "PATRON000014", "arjun.rao@patron.example", ""),
    ("Elena Petrova", "PATRON000015", "", "+15550100015"),
    ("Kwame Asante", "PATRON000016", "kwame.asante@patron.example", ""),
    ("Isabel Cruz", "PATRON000017", "isabel.cruz@patron.example", "+15550100017"),
    ("Oskar Lind", "PATRON000018", "oskar.lind@patron.example", ""),
    ("Chloe Bennett", "PATRON000019", "", "+15550100019"),
    ("Yusuf Demir", "", "yusuf.demir@patron.example", ""),  # joined this week; his card comes two days later
]
JOINED = [-92, -90, -88, -85, -80, -76, -70, -66, -62, -55, -50, -46, -40, -3]  # days ago each of them joined
LOCATIONS = {
    "Main Library": ("General collection, ground floor", 90),
    "Technology Section": ("Computing and engineering, first floor", 80),
    "Science Section": ("Natural sciences, first floor", 70),
    "Humanities Section": ("History, literature and the arts, second floor", 60),
    "Reference Room": ("Dictionaries, atlases and handbooks: not for loan", 12),
    "New Arrivals": ("Books received in the last month, by the entrance", 30),
}
TITLE_WORDS = (
    ["Quiet", "Hidden", "Northern", "Silver", "Practical", "Modern", "Forgotten", "Coastal", "Urban", "Gentle"],
    ["Harbours", "Gardens", "Algorithms", "Rivers", "Lanterns", "Orchards", "Machines", "Tides", "Bridges", "Maps"],
)
FIRST = ["Asha", "Bruno", "Carmen", "Dmitri", "Esther", "Farid", "Greta", "Hiro", "Ines", "Jamal", "Katya", "Luis"]
LAST = ["Abara", "Bianchi", "Castell", "Dunmore", "Eriksen", "Fontaine", "Gallo", "Hart", "Ibsen", "Joshi", "Kerr"]


class DemoSeedError(RuntimeError):
    pass


# ------------------------------------------------------------------ calling the application in this process
class Api:
    """Sends requests to the ASGI application directly, without a server or a network socket."""

    def __init__(self, app) -> None:
        self.app = app
        self.loop = asyncio.new_event_loop()
        self.tokens: dict[str, str] = {}
        self.last_status = 0

    def close(self) -> None:
        self.loop.close()

    async def _asgi(self, method: str, path: str, query: str, headers: list, body: bytes) -> tuple[int, bytes]:
        done, status, chunks, sent = asyncio.Event(), [0], [], [False]

        async def receive():
            if not sent[0]:
                sent[0] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    done.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
            "headers": headers, "client": ("127.0.0.1", 50000), "server": ("localhost", 8000),
        }  # fmt: skip
        await self.app(scope, receive, send)
        return status[0], b"".join(chunks)

    def call(
        self,
        who: str | None,
        method: str,
        path: str,
        body: Any = None,
        *,
        params: dict | None = None,
        upload: tuple[str, bytes] | None = None,
        ok: tuple[int, ...] = (200, 201),
    ) -> Any:
        headers = [(b"host", b"localhost")]
        data = b""
        if who is not None:
            headers.append((b"authorization", f"Bearer {self.token(who)}".encode()))
        if upload is not None:
            boundary = uuid.uuid4().hex
            name, content = upload
            data = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).encode()
                + content
                + f"\r\n--{boundary}--\r\n".encode()
            )
            headers.append((b"content-type", f"multipart/form-data; boundary={boundary}".encode()))
        elif body is not None:
            data = json.dumps(body).encode()
            headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(data)).encode()))
        status, raw = self.loop.run_until_complete(self._asgi(method, path, urlencode(params or {}), headers, data))
        self.last_status = status
        try:
            out = json.loads(raw) if raw else None
        except ValueError:
            out = raw.decode("utf-8", "replace")
        if ok and status not in ok:
            raise DemoSeedError(f"{method} {path} as {who}: {status} {str(out)[:400]}")
        return out

    def token(self, who: str) -> str:
        if who not in self.tokens:
            r = self.call(None, "POST", "/api/auth/card-login", {"uid": card_of(who)})
            self.tokens[who] = r["token"]
        return self.tokens[who]

    def sign_out_everyone(self) -> None:
        for who in list(self.tokens):
            self.call(who, "POST", "/api/auth/logout")
        self.tokens.clear()


_today_card: dict[str, str] = {}


def card_of(who: str) -> str:
    return _today_card.get(who) or STAFF_CARDS[who]


# ------------------------------------------------------------------ the timeline
@dataclass(order=True)
class Step:
    when: datetime
    seq: int
    label: str = field(compare=False)
    run: Callable[[], None] = field(compare=False)


@dataclass
class World:
    """What the seed knows while it replays the timeline (ids it needs later)."""

    rng: random.Random
    imported: list[int] = field(default_factory=list)  # item ids from the spreadsheet
    extra: list[int] = field(default_factory=list)  # item ids catalogued by hand
    members: list[str] = field(default_factory=list)  # member numbers, in order
    orders: dict[str, int] = field(default_factory=dict)
    received: list[int] = field(default_factory=list)  # item ids booked in from orders and serials
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


def _seed_settings(settings: Settings) -> Settings:
    """The site's database and rules, but simulated devices, ILMS, catalogue source and message providers: demo data
    must never reach a real ILMS, gate or mailbox."""
    return replace(
        settings,
        environment="development",
        devices=DEFAULT_DEVICES,
        netcat_driver="mock",
        opac_kiosk_hosts=(),
        circulation=replace(settings.circulation, ilms_driver="mock", ilms_backoff_s=0.0),
        notifications=NotificationConfig(backoff_s=0.0),
        gate=GateConfig(),
    )


def catalogue_size(settings: Settings) -> int:
    from aisys.db import make_engine, make_sessionmaker

    with make_sessionmaker(make_engine(settings.database_url))() as db:
        return db.scalar(select(func.count(Item.id))) or 0


def seed_full_demo(settings: Settings, *, quiet: bool = False) -> dict[str, int]:
    """Replay the demo library's history. The catalogue must be empty (the staff and patron cards may exist)."""
    from aisys.app import create_app
    from aisys.cli import seed_demo

    if catalogue_size(settings):
        raise DemoSeedError("The catalogue already has items: the full demo library only goes into an empty one")
    now = datetime.now(UTC)
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)

    def at(day: int, hh: int = 10, mm: int = 0) -> datetime:
        """A moment `day` days from today (0 = today, which is counted back from now so it is never in the future)."""
        if day >= 0:
            return now - timedelta(minutes=max(5, 180 - hh * 10 - mm // 6))
        return midnight + timedelta(days=day, hours=hh, minutes=mm)

    with clock_at(at(-100, 8, 0)):
        seed_demo(settings)  # the four staff of the sign-in page and the first six patrons
    app = create_app(_seed_settings(settings))
    api = Api(app)
    world = World(rng=random.Random(2026))
    steps: list[Step] = []

    def plan(when: datetime, label: str, run: Callable[[], None]) -> None:
        steps.append(Step(when, len(steps), label, run))

    _plan_staff_and_shelves(plan, at, api, world)
    _plan_catalogue(plan, at, api, world, app)
    _plan_patrons(plan, at, api, world)
    _plan_circulation(plan, at, api, world, app)
    _plan_collection_care(plan, at, api, world, app)
    _plan_acquisitions_and_serials(plan, at, api, world)
    _plan_gate_inventory_and_messages(plan, at, api, world, app)
    _plan_administration(plan, at, api, world)

    day, last = None, None
    try:
        for step in sorted(steps):
            if day is not None and last is not None and step.when.date() != day:
                closing = max(datetime(day.year, day.month, day.day, 17, 45, tzinfo=UTC), last + timedelta(minutes=5))
                with clock_at(min(closing, step.when - timedelta(minutes=1))):
                    api.sign_out_everyone()  # the end of a working day
            day, last = step.when.date(), step.when
            if not quiet:
                print(f"  {step.when:%Y-%m-%d %H:%M}  {step.label}")
            with clock_at(step.when):
                step.run()
        api.sign_out_everyone()
    finally:
        api.close()
    with app.state.session_factory() as db:
        world.counts["items"] = db.scalar(select(func.count(Item.id))) or 0
        world.counts["untagged"] = (
            db.scalar(
                select(func.count(Item.id)).where(
                    ~Item.id.in_(
                        select(RfidTag.item_id).where(RfidTag.status == "active", RfidTag.item_id.is_not(None))
                    )
                )
            )
            or 0
        )
        world.counts["patrons"] = db.scalar(select(func.count(Member.id))) or 0
        world.counts["open_loans"] = db.scalar(select(func.count(Loan.id)).where(Loan.returned_at.is_(None))) or 0
        world.counts["past_loans"] = db.scalar(select(func.count(Loan.id)).where(Loan.returned_at.is_not(None))) or 0
    return world.counts


# ------------------------------------------------------------------ staff, shelves
def _plan_staff_and_shelves(plan, at, api: Api, world: World) -> None:
    def staff() -> None:
        for username, name, role, cards in EXTRA_STAFF:
            api.call("admin", "POST", "/api/admin/users", {"username": username, "display_name": name, "role": role})
            api.call("admin", "POST", "/api/admin/cards", {"uid": cards[0], "kind": "staff", "username": username})
        _today_card["jonas"] = "STAFFCLERK3X"
        people = api.call("admin", "GET", "/api/admin/people")["people"]
        noor = next(p for p in people if p["username"] == "noor")
        api.call(
            "admin", "PUT", f"/api/notifications/prefs/{noor['id']}",
            {"channels": {"email": {"active": True, "address": "noor.haddad@library.example"},
                          "sms": {"active": True, "address": "+15550100902"}}},
        )  # fmt: skip

    plan(at(-100, 9, 0), "administrator adds the second librarian and two more desk clerks", staff)

    def shelves() -> None:
        for code, (description, capacity) in LOCATIONS.items():
            api.call("leo", "POST", "/api/shelves", {"code": code, "description": description, "capacity": capacity})

    plan(at(-99, 9, 30), "librarian sets up the shelves", shelves)


# ------------------------------------------------------------------ catalogue: import, tagging, extra books
def _make_book(rng: random.Random, n: int, location: str) -> dict:
    adj, noun = rng.choice(TITLE_WORDS[0]), rng.choice(TITLE_WORDS[1])
    pattern = rng.choice(["The {a} {n}", "{n} of the {a} Coast", "A Field Guide to {a} {n}", "{a} {n} Revisited"])
    first, last = rng.choice(FIRST), rng.choice(LAST)
    reference = location == "Reference Room"
    return {
        "title": pattern.format(a=adj, n=noun) + f", Part {n % 7 + 1}",
        "accession_no": f"ACC2{n:05d}",
        "barcode": f"BC2{n:05d}",
        "author": f"{first} {last}",
        "isbn": sample_data.isbn13(rng),
        "publisher": rng.choice(["Lakeside Books", "Harbour & Finch", "Northgate Press", "Quiet Press"]),
        "pub_year": rng.randint(1995, 2025),
        "call_number": f"{rng.randint(1, 999):03d}.{rng.randint(1, 99)} {last[:3].upper()}",
        "location": location,
        "item_type": "reference" if reference else "book",
        "price": f"{rng.randint(250, 2400)}.00",
    }


def _tag(api: Api, who: str, item_id: int, world: World) -> bool:
    uid = api.call(who, "POST", "/api/rfid/mock/readers/staff-1/tags", {"replace": True})["uid"]
    api.call(who, "POST", "/api/rfid/tagging", {"item_id": item_id, "tag_uid": uid}, ok=())
    done = api.last_status == 201  # a lost or withdrawn record from the old system is not tagged
    world.bump("tagged" if done else "not_tagged")
    return done


def _plan_catalogue(plan, at, api: Api, world: World, app) -> None:
    def import_sample() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            headers, rows, _ = sample_data.build_rows(200, 42)
            path = Path(tmp) / "library_200.xlsx"
            sample_data.write_xlsx(path, headers, rows)
            data = path.read_bytes()
        batch = api.call("leo", "POST", "/api/migrations", upload=("library_200.xlsx", data))
        api.call("leo", "POST", f"/api/migrations/{batch['id']}/commit")
        with app.state.session_factory() as db:
            world.imported = list(db.scalars(select(Item.id).order_by(Item.id)))

    plan(at(-98, 10, 0), "librarian imports the old system's spreadsheet (library_200.xlsx)", import_sample)

    def trial_import() -> None:
        body = (
            "Accession,Title,Author,Location\nDON-001,Donated Atlas of Rivers,Mira Holt,Main Library\n"
            "DON-002,Donated Field Notes,Paul Avery,Main Library\nDON-003,Donated Star Charts,Ivy Lam,Main Library\n"
        )
        batch = api.call("noor", "POST", "/api/migrations", upload=("donations_spring.csv", body.encode()))
        api.call("noor", "POST", f"/api/migrations/{batch['id']}/commit")
        api.call("noor", "POST", f"/api/migrations/{batch['id']}/rollback")  # entered on the wrong form: undone

    plan(at(-97, 15, 10), "second librarian tries a donations file, then rolls it back", trial_import)

    thirds = 3
    for part in range(thirds):

        def tag_part(part=part) -> None:
            ids = world.imported[part::thirds]
            who = "leo" if part != 1 else "noor"
            for item_id in ids:
                _tag(api, who, item_id, world)

        plan(at(-97 + part, 11, 0), f"tagging the imported books ({part + 1} of {thirds})", tag_part)

    locations = list(LOCATIONS)[:5]
    for batch_no, (day, who, untagged) in enumerate([(-93, "noor", 8), (-60, "leo", 7)]):

        def catalogue_more(batch_no=batch_no, who=who, untagged=untagged) -> None:
            for k in range(30):
                n = batch_no * 30 + k + 1
                book = _make_book(world.rng, n, locations[n % len(locations)])
                item = api.call(who, "POST", "/api/catalogue/items", book)
                world.extra.append(item["id"])
                if k >= untagged:
                    _tag(api, who, item["id"], world)

        plan(at(day, 14, 0), f"{who} catalogues 30 more books by hand ({untagged} left untagged)", catalogue_more)

    def barcode_labels() -> None:
        api.call(
            "leo", "POST", "/api/labels/print", {"kind": "barcode", "printer": "front-desk", "ids": world.extra[30:]}
        )

    plan(at(-59, 9, 40), "librarian prints barcode labels for the new books", barcode_labels)


# ------------------------------------------------------------------ patrons
def _plan_patrons(plan, at, api: Api, world: World) -> None:
    world.members = [f"M{n:04d}" for n in range(1, 7)]
    for (name, card, email, phone), day in zip(EXTRA_PATRONS, JOINED, strict=True):

        def join(name=name, card=card, email=email, phone=phone) -> None:
            who = "noor" if card.endswith(("1", "3", "5", "7", "9")) else "leo"
            m = api.call(who, "POST", "/api/circulation/members",
                         {"name": name, "card_uid": card, "email": email, "phone": phone})  # fmt: skip
            world.members.append(m["member_no"])

        plan(at(day, 11, 30), f"new patron {name}", join)

    def contact_changes() -> None:
        api.call("noor", "PATCH", "/api/circulation/members/M0012/contact",
                 {"email": "lucas.moreau@patron.example", "reminders_on": False})  # fmt: skip
        api.call("leo", "PATCH", "/api/circulation/members/M0006/contact", {"phone": "+15550100006"})

    plan(at(-45, 12, 0), "contact details updated; one patron asks for no reminders", contact_changes)

    def replace_card() -> None:
        api.call("noor", "POST", "/api/circulation/members/M0008/card/validate", {"uid": "PATRON000108"})
        api.call("noor", "POST", "/api/circulation/members/M0008/card", {"uid": "PATRON000108"})

    plan(at(-25, 10, 20), "Daniel Brooks lost his card: a replacement is issued", replace_card)

    def personalise() -> None:
        for m in ("M0007", "M0013"):
            api.call("leo", "POST", f"/api/circulation/members/{m}/card/personalise")

    plan(at(-44, 15, 0), "two patron cards printed and encoded on the card printer", personalise)


# ------------------------------------------------------------------ circulation history and current loans
def _plan_circulation(plan, at, api: Api, world: World, app) -> None:
    rng = random.Random(7)
    clerks = ["cleo", "ravi", "jonas"]
    pool: list[str] = []
    owes = {"M0014"}  # leaves this patron's first late fine unpaid

    def ready_pool() -> None:
        with app.state.session_factory() as db:
            rows = db.execute(
                select(Item.accession_no)
                .join(RfidTag, (RfidTag.item_id == Item.id) & (RfidTag.status == "active"))
                .where(Item.status == "available", Item.is_reference.is_(False), Item.id.in_(world.imported))
                .order_by(Item.id)
            ).all()
        pool.extend(r[0] for r in rows)
        rng.shuffle(pool)

    plan(at(-89, 8, 30), "desk opens for lending", ready_pool)

    def take() -> str:
        return pool.pop()

    def clerk() -> str:
        return rng.choice(clerks)

    def lend(member: str, acc_box: list, day: int) -> Callable[[], None]:
        def run() -> None:
            acc_box.append(take())
            if rng.random() < 0.3:
                api.call(clerk(), "POST", "/api/circulation/members/identify", {"uid": _card_uid(member)}, ok=())
            desk = clerk()
            api.call(desk, "POST", "/api/circulation/mock/present", {"item_ref": acc_box[0]})  # book on the reader pad
            api.call(desk, "POST", "/api/circulation/checkout", {"member_no": member, "item_ref": acc_box[0]})
            world.bump("checkouts")

        return run

    def give_back(member: str, acc_box: list, day: int) -> Callable[[], None]:
        def run() -> None:
            desk = clerk()
            api.call(desk, "POST", "/api/circulation/mock/present", {"item_ref": acc_box[0]})
            out = api.call(desk, "POST", "/api/circulation/checkin", {"item_ref": acc_box[0]})
            world.bump("returns")
            fine = (out.get("fine") or {}).get("amount_cents", 0)
            fines = f"/api/circulation/members/{member}/fines"
            if fine and member == "M0010":
                note = "Returned through the book drop on time; drop not emptied"
                api.call("leo", "POST", f"{fines}/waiver", {"amount_cents": fine, "note": note})
            elif fine and member in owes:
                owes.discard(member)
            elif fine:
                api.call("noor", "POST", f"{fines}/payment", {"amount_cents": fine, "note": "Paid at the desk"})

        return run

    def renew(acc_box: list, day: int) -> Callable[[], None]:
        def run() -> None:
            api.call(clerk(), "POST", "/api/circulation/renew", {"item_ref": acc_box[0]})
            world.bump("renewals")

        return run

    # past loans: taken between 88 and 45 days ago, all back at least 23 days ago
    history_members = ["M0001", "M0002", "M0005", "M0006", "M0007", "M0008", "M0009", "M0010", "M0011",
                       "M0013", "M0014", "M0016", "M0017", "M0018"]  # fmt: skip
    for member in history_members:
        joined = -100 if member <= "M0006" else JOINED[int(member[1:]) - 7]
        late_joiner = joined > -48
        for _ in range(rng.randint(1, 2) if late_joiner else rng.randint(2, 4)):
            # back by 23 days ago (18 for the two who joined late), before any current loan begins
            start = rng.randint(max(-88, joined + 2), -40 if late_joiner else -45)
            length = rng.choice([5, 8, 11, 13, 16, 19, 22])
            box: list = []
            plan(at(start, 10, rng.randint(0, 59)), f"{member} borrows a book", lend(member, box, start))
            if length > 14 and rng.random() < 0.5:
                plan(at(start + 12, 12, rng.randint(0, 59)), f"{member} renews", renew(box, start + 12))
            plan(
                at(start + length, 15, rng.randint(0, 59)),
                f"{member} returns a book",
                give_back(member, box, start + length),
            )

    # one return a week late whose fine is left to pay, so the patron list has someone owing a little
    late: list = []
    plan(at(-52, 10, 5), "M0014 borrows a book", lend("M0014", late, -52))
    plan(at(-30, 16, 0), "M0014 returns it a week late; the fine is left to pay", give_back("M0014", late, -30))

    # M0003 was blocked; unblocked when she returned everything, borrowed again, and is blocked again for not returning
    def unblock() -> None:
        api.call(
            "leo",
            "POST",
            "/api/circulation/members/M0003/block",
            {"blocked": False, "reason": "Overdue items returned"},
        )

    plan(at(-60, 9, 15), "M0003 unblocked", unblock)
    for k in range(2):
        plan(at(-40, 11, 10 + k), "M0003 borrows a book", lend("M0003", [], -40))

    def reblock() -> None:
        api.call("leo", "POST", "/api/circulation/members/M0003/block",
                 {"blocked": True, "reason": "Two items over a week overdue; letter sent"})  # fmt: skip

    plan(at(-18, 9, 5), "M0003 blocked again", reblock)

    def refused() -> None:
        r = api.call(
            clerk(), "POST", "/api/circulation/checkout", {"member_no": "M0004", "item_ref": pool[-1]}, ok=(409,)
        )
        world.bump("refusals")
        assert "fee" in str(r).lower() or "fine" in str(r).lower(), r

    plan(at(-30, 14, 20), "M0004 is refused: fines over the limit", refused)

    def outage() -> None:
        api.call("cleo", "POST", "/api/circulation/mock/ilms/fail", {"mode": "timeout", "times": 10})
        api.call(
            "cleo",
            "POST",
            "/api/circulation/checkout",
            {"member_no": "M0001", "item_ref": pool[-1]},
            ok=(409, 502, 503, 504),
        )
        api.call("cleo", "DELETE", "/api/circulation/mock/ilms/fail")
        world.bump("ilms_failures")

    plan(at(-16, 13, 0), "the ILMS does not answer for a few minutes: one check-out fails cleanly", outage)

    # current loans: taken in the last 19 days; those older than 14 days and not renewed are overdue now
    current = {"M0001": 3, "M0002": 2, "M0005": 1, "M0006": 2, "M0007": 4, "M0009": 2, "M0010": 1, "M0011": 2,
               "M0013": 3, "M0014": 1, "M0015": 2, "M0016": 1, "M0017": 2, "M0019": 2}  # fmt: skip
    for member, n in current.items():
        for _ in range(n):
            start = rng.randint(-19, -1)
            box = []
            plan(at(start, 10, rng.randint(0, 59)), f"{member} borrows a book", lend(member, box, start))
            if -13 <= start <= -10 and rng.random() < 0.6:
                plan(at(-2, 11, rng.randint(0, 59)), f"{member} renews", renew(box, -2))

    # Oskar Lind moves away: his loans are back, so his record is removed (kept for the history, hidden from lists)
    def remove_member() -> None:
        api.call("leo", "POST", "/api/members/M0018/remove", {"reason": "Moved away; asked to close the account"})

    plan(at(-12, 16, 0), "a patron who moved away is removed", remove_member)

    def late_card() -> None:
        api.call("noor", "POST", "/api/circulation/members/M0020/card", {"uid": "PATRON000020"})

    plan(at(-2, 10, 0), "Yusuf Demir's card is issued", late_card)


def _card_uid(member_no: str) -> str:
    return "PATRON000108" if member_no == "M0008" else f"PATRON{int(member_no[1:]):06d}"


# ------------------------------------------------------------------ catalogue corrections, damaged, lost, tags
def _plan_collection_care(plan, at, api: Api, world: World, app) -> None:
    def pick_available(n: int, skip: int) -> list[tuple[int, str]]:
        with app.state.session_factory() as db:
            rows = db.execute(
                select(Item.id, RfidTag.tag_uid)
                .join(RfidTag, (RfidTag.item_id == Item.id) & (RfidTag.status == "active"))
                .where(Item.status == "available", Item.is_reference.is_(False), Item.id.in_(world.extra))
                .order_by(Item.id.desc())
                .offset(skip)
                .limit(n)
            ).all()
        return [(r[0], r[1]) for r in rows]

    def care() -> None:
        (damaged, _), (lost, _), (withdrawn, wtag), (moved, _) = pick_available(4, 3)
        api.call(
            "leo",
            "PATCH",
            f"/api/catalogue/items/{damaged}",
            {"status": "damaged", "reason": "Water damage; sent for repair"},
        )
        api.call(
            "leo",
            "PATCH",
            f"/api/catalogue/items/{lost}",
            {"status": "lost", "reason": "Not found in two stock checks"},
        )
        api.call(
            "leo", "PATCH", f"/api/catalogue/items/{withdrawn}", {"status": "withdrawn", "reason": "Worn out; replaced"}
        )
        api.call("leo", "POST", f"/api/rfid/tags/{wtag}/remove", {"reason": "Book withdrawn; tag kept for reuse"})
        api.call("noor", "PATCH", f"/api/catalogue/items/{moved}",
                 {"location": "Technology Section", "reason": "Belongs with the computing books"})  # fmt: skip
        world.bump("catalogue_edits", 4)

    plan(at(-35, 9, 50), "stock check follow-up: damaged, lost, withdrawn, re-shelved", care)

    def retag() -> None:
        [(item_id, _)] = pick_available(1, 9)
        uid = api.call("noor", "POST", "/api/rfid/mock/readers/staff-1/tags", {"replace": True})["uid"]
        api.call("noor", "POST", "/api/rfid/tagging",
                 {"item_id": item_id, "tag_uid": uid, "replace": True, "old_tag_fate": "retired",
                  "reason": "Old tag cracked; reads only now and then"})  # fmt: skip

    plan(at(-21, 11, 45), "a cracked tag is replaced and retired", retag)


# ------------------------------------------------------------------ acquisitions and serials
def _order_line(rng: random.Random, title: str, author: str, quantity: int) -> dict:
    return {"title": title, "author": author, "isbn": sample_data.isbn13(rng), "publisher": "Northlight Books",
            "pub_year": 2025, "quantity": quantity, "unit_price_cents": rng.randint(900, 3200),
            "location": "New Arrivals"}  # fmt: skip


def _plan_acquisitions_and_serials(plan, at, api: Api, world: World) -> None:
    rng = random.Random(11)

    def order(key: str, supplier: str, note: str, lines: list[dict], who: str = "noor") -> None:
        o = api.call(who, "POST", "/api/acquisitions", {"supplier": supplier, "note": note, "lines": lines})
        world.orders[key] = o["id"]

    def receive(key: str, how_many: Callable[[dict], int], who: str = "noor") -> None:
        o = api.call(who, "GET", f"/api/acquisitions/{world.orders[key]}")
        receipts = [{"line_id": line["id"], "quantity": q} for line in o["lines"] if (q := how_many(line))]
        out = api.call(who, "POST", f"/api/acquisitions/{world.orders[key]}/receive", {"receipts": receipts})
        for c in out["created"]:
            world.received.append(c["item_id"])
            _tag(api, who, c["item_id"], world)

    plan(at(-40, 10, 0), "order PO-1 drafted", lambda: order("po1", "Northlight Books", "Spring reading list", [
        _order_line(rng, "The Lighthouse Ledger", "Marit Solheim", 2),
        _order_line(rng, "Small Engines of Thought", "Tomas Hale", 1),
        _order_line(rng, "A Year of Tides", "June Okoro", 1)]))  # fmt: skip
    plan(
        at(-39, 9, 0),
        "order PO-1 placed",
        lambda: api.call("noor", "POST", f"/api/acquisitions/{world.orders['po1']}/place"),
    )
    plan(
        at(-25, 11, 0), "order PO-1 received in full and tagged", lambda: receive("po1", lambda line: line["quantity"])
    )
    plan(at(-15, 10, 0), "order PO-2 drafted", lambda: order("po2", "Harbour & Finch", "Out of print in two weeks", [
        _order_line(rng, "Maps for Quiet Rooms", "Selma Varga", 1)]))  # fmt: skip
    plan(at(-14, 9, 30), "order PO-2 cancelled: supplier out of stock",
         lambda: api.call("noor", "POST", f"/api/acquisitions/{world.orders['po2']}/cancel"))  # fmt: skip
    plan(at(-20, 14, 0), "order PO-3 drafted", lambda: order("po3", "Lakeside Books", "Course texts for next term", [
        _order_line(rng, "Introduction to Gentle Algorithms", "Priyanka Dutt", 3),
        _order_line(rng, "Bridges and Their Builders", "Owen Price", 2)], who="leo"))  # fmt: skip
    plan(
        at(-19, 9, 0),
        "order PO-3 placed",
        lambda: api.call("leo", "POST", f"/api/acquisitions/{world.orders['po3']}/place"),
    )
    plan(at(-6, 12, 0), "order PO-3 partly received (2 of 5 copies)",
         lambda: receive("po3", lambda line: 2 if line["quantity"] == 3 else 0, who="leo"))  # fmt: skip
    plan(at(-4, 10, 0), "order PO-4 drafted", lambda: order("po4", "Northgate Press", "Reference refresh", [
        _order_line(rng, "Atlas of Northern Rivers", "Hedda Lunde", 1)]))  # fmt: skip
    plan(
        at(-4, 10, 30),
        "order PO-4 placed",
        lambda: api.call("noor", "POST", f"/api/acquisitions/{world.orders['po4']}/place"),
    )
    wish = [_order_line(rng, "The Orchard Keeper's Year", "Alma Reyes", 1)]
    plan(at(-2, 15, 0), "order PO-5 drafted (not placed yet)",
         lambda: order("po5", "Quiet Press", "Wish list from patrons", wish, who="leo"))  # fmt: skip

    serials: dict[str, int] = {}
    today = datetime.now(UTC).date()

    def subscribe(key: str, body: dict) -> None:
        serials[key] = api.call("noor", "POST", "/api/serials", body)["id"]

    def issues(key: str) -> list[dict]:
        return api.call("noor", "GET", f"/api/serials/{serials[key]}")["issues"]

    def receive_issue(key: str, n: int, received: date, book_in: bool = False) -> None:
        issue = issues(key)[n]
        out = api.call("noor", "POST", f"/api/serials/issues/{issue['id']}/receive",
                       {"received_on": received.isoformat(), "book_in": book_in})  # fmt: skip
        if out.get("item_id"):
            world.received.append(out["item_id"])

    def mark(key: str, n: int, status: str, note: str) -> None:
        issue = issues(key)[n]
        api.call("noor", "POST", f"/api/serials/issues/{issue['id']}/mark", {"status": status, "note": note})

    def start_subscriptions() -> None:
        for key, title, issn, frequency, location, first, ahead in (
            ("jlt", "Journal of Library Technology", "0378-5955", "monthly", "Main Library", 95, 6),
            ("csq", "Coastal Science Quarterly", "2049-3630", "quarterly", "Science Section", 180, 3),
        ):
            subscribe(key, {"title": title, "issn": issn, "frequency": frequency, "supplier": "Periodicals Ltd",
                            "location": location, "first_expected_on": (today - timedelta(days=first)).isoformat(),
                            "issues_ahead": ahead})  # fmt: skip

    plan(at(-96, 10, 0), "serial subscriptions started", start_subscriptions)
    plan(at(-30, 9, 0), "weekly subscription started", lambda: subscribe("wrd", {
        "title": "The Weekly Reader's Digest", "issn": "0317-8471", "frequency": "weekly", "supplier": "NewsStand Co",
        "location": "Humanities Section", "first_expected_on": (today - timedelta(days=28)).isoformat(),
        "issues_ahead": 6}))  # fmt: skip
    plan(at(-93, 11, 0), "JLT issue 1 received", lambda: receive_issue("jlt", 0, today - timedelta(days=93), True))
    plan(at(-64, 11, 0), "JLT issue 2 received", lambda: receive_issue("jlt", 1, today - timedelta(days=64)))
    plan(
        at(-25, 10, 0),
        "JLT issue 3 claimed from the supplier",
        lambda: mark("jlt", 2, "claimed", "Not arrived; claim sent"),
    )
    plan(
        at(-19, 10, 0),
        "JLT issue 3 arrives after the claim",
        lambda: receive_issue("jlt", 2, today - timedelta(days=19), True),
    )
    plan(at(-95, 10, 0), "CSQ issue 1 arrives very late", lambda: receive_issue("csq", 0, today - timedelta(days=95)))
    plan(
        at(-10, 10, 0),
        "CSQ issue 2 marked missing",
        lambda: mark("csq", 1, "missing", "Supplier says it was never printed"),
    )
    plan(at(-27, 9, 30), "weekly issue 1 received", lambda: receive_issue("wrd", 0, today - timedelta(days=27)))
    plan(at(-20, 9, 30), "weekly issue 2 received", lambda: receive_issue("wrd", 1, today - timedelta(days=20)))

    def spine_labels() -> None:
        api.call(
            "leo", "POST", "/api/labels/print", {"kind": "spine", "printer": "front-desk", "location": "New Arrivals"}
        )

    plan(at(-5, 9, 10), "spine labels printed for the New Arrivals shelf", spine_labels)


# ------------------------------------------------------------------ gate, inventory, messages
def _plan_gate_inventory_and_messages(plan, at, api: Api, world: World, app) -> None:
    def on_loan(n: int) -> list[str]:
        with app.state.session_factory() as db:
            return list(
                db.scalars(
                    select(Item.accession_no)
                    .join(Loan, Loan.item_id == Item.id)
                    .where(Loan.returned_at.is_(None))
                    .limit(n)
                )
            )

    def on_shelf(n: int, skip: int = 0) -> list[str]:
        with app.state.session_factory() as db:
            return list(
                db.scalars(
                    select(Item.accession_no)
                    .join(RfidTag, (RfidTag.item_id == Item.id) & (RfidTag.status == "active"))
                    .where(Item.status == "available")
                    .order_by(Item.id)
                    .offset(skip)
                    .limit(n)
                )
            )

    def walk(body: dict) -> Callable[[], None]:
        def run() -> None:
            refs = body.get("item_refs")
            payload = {**body, "item_refs": refs() if callable(refs) else refs or []}
            api.call("leo", "POST", "/api/gate/mock/gates/gate-1/walk", payload)
            world.bump("gate_walks")

        return run

    for day, hh in ((-6, 10), (-5, 9), (-3, 11), (-1, 16), (0, 2)):
        plan(at(day, hh, 5), "visitors walk through the gate", walk({"people": 3 + day % 3}))
    plan(at(-6, 11, 2), "a patron leaves with borrowed books: no alarm", walk({"item_refs": lambda: on_loan(2)}))
    plan(at(-5, 16, 40), "an unissued book goes through the gate: alarm, snapshot, alerts",
         walk({"item_refs": lambda: on_shelf(1, 40), "people": 1}))  # fmt: skip

    def camera_down() -> None:
        api.call("leo", "POST", "/api/gate/mock/camera/fail")

    plan(at(-3, 12, 19), "the CCTV camera stops answering", camera_down)
    plan(at(-3, 12, 20), "alarm while the camera is down: recorded without a snapshot",
         walk({"item_refs": lambda: on_shelf(1, 55), "people": 2}))  # fmt: skip
    plan(at(-2, 9, 5), "a tag from another library sets off the gate", walk({"unregistered": 1}))
    plan(at(-1, 18, 10), "two borrowed books and one not issued: alarm",
         walk({"item_refs": lambda: on_loan(2) + on_shelf(1, 70), "people": 2}))  # fmt: skip

    def dispatch() -> None:
        api.call("admin", "POST", "/api/notifications/dispatch")

    def reminders() -> None:
        api.call("admin", "POST", "/api/notifications/reminders/run")

    for day in (-5, -3, -1):
        plan(at(day, 17, 0), "messages sent (gate alerts, reminders)", dispatch)
    plan(at(-10, 8, 0), "reminders queued", reminders)
    plan(at(-3, 8, 0), "reminders queued", reminders)

    def failing_sms() -> None:
        api.call("admin", "POST", "/api/notifications/mock/fail", {"channel": "sms", "mode": "error", "times": 10})
        reminders()
        dispatch()
        api.call("admin", "DELETE", "/api/notifications/mock/fail")

    plan(at(0, 6, 0), "this morning's reminders: the SMS gateway is failing, texts wait for a retry", failing_sms)

    def sweep(location: str, who: str, close: bool, **planted) -> Callable[[], None]:
        def run() -> None:
            s = api.call(who, "POST", "/api/inventory/sessions", {"location": location})
            api.call(
                who, "POST", "/api/inventory/mock/handhelds/handheld-1/load-shelf", {"session_id": s["id"], **planted}
            )
            api.call(who, "POST", f"/api/inventory/sessions/{s['id']}/scan")
            if close:
                api.call(who, "POST", f"/api/inventory/sessions/{s['id']}/close")
            world.bump("inventory_sessions")

        return run

    plan(
        at(-50, 8, 30),
        "stock check of the Humanities Section",
        sweep("Humanities Section", "leo", True, missing=1, strays=0, unknown=0, duplicates=1),
    )
    plan(at(-8, 8, 30), "stock check of the Science Section",
         sweep("Science Section", "leo", True, missing=2, strays=1, unknown=1, duplicates=2))  # fmt: skip
    plan(at(0, 4, 0), "stock check of the Reference Room (still open)",
         sweep("Reference Room", "noor", False, missing=1, strays=1, unknown=0, duplicates=1))  # fmt: skip


# ------------------------------------------------------------------ administration
def _plan_administration(plan, at, api: Api, world: World) -> None:
    def lost_staff_card() -> None:
        people = api.call("admin", "GET", "/api/admin/people")["people"]
        jonas = next(p for p in people if p["username"] == "jonas")
        old = next(c for c in jonas["cards"] if c["active"])
        api.call("admin", "PATCH", f"/api/admin/cards/{old['id']}", {"active": False})
        api.call("admin", "POST", "/api/admin/cards", {"uid": "STAFFCLERK03", "kind": "staff", "username": "jonas"})
        api.tokens.pop("jonas", None)
        _today_card["jonas"] = "STAFFCLERK03"

    plan(at(JONAS_CARD_LOST_DAY, 8, 45), "a clerk's staff card is lost: switched off and replaced", lost_staff_card)

    def mistyped_password() -> None:
        api.call(None, "POST", "/api/auth/login", {"username": "admin", "password": "not-the-password"}, ok=(401,))

    plan(at(-11, 8, 2), "a mistyped administrator password (refused and audited)", mistyped_password)

    def settings_changes() -> None:
        api.call("admin", "PUT", "/api/admin/settings", {"values": {"circulation.max_renewals": 3}})

    plan(at(-3, 9, 0), "loan rules: three renewals allowed from now on", settings_changes)

    def trial_fine() -> None:
        api.call("admin", "PUT", "/api/admin/settings", {"values": {"circulation.fine_per_day_cents": 30}})

    def undo_fine() -> None:
        api.call("admin", "DELETE", "/api/admin/settings/circulation.fine_per_day_cents")

    plan(at(-2, 9, 0), "fine per day raised for a trial", trial_fine)
    plan(at(-1, 9, 0), "fine per day set back to the file value", undo_fine)

    def staged_batch() -> None:
        body = "Accession,Title,Author\nFIX-1,Corrected Title One,A. Writer\n,No accession number,B. Writer\n"
        api.call("noor", "POST", "/api/migrations", upload=("catalogue_corrections.csv", body.encode()))

    plan(at(-1, 14, 0), "a corrections file is checked but not imported yet", staged_batch)

    def backups() -> None:
        api.call("admin", "POST", "/api/backups", {"label": "demo-seed", "note": "After the demo library was loaded"})

    plan(at(0, 8, 0), "a backup is made", backups)

    def auditor() -> None:
        api.call("ari", "GET", "/api/audit/events", params={"limit": 50})
        api.call("ari", "GET", "/api/audit/export.csv")
        api.call("ari", "GET", "/api/reports")

    plan(at(0, 9, 0), "the auditor reviews the audit log", auditor)


def report(counts: dict[str, int]) -> str:
    keys = ("items", "untagged", "patrons", "past_loans", "open_loans", "renewals", "gate_walks", "inventory_sessions")
    return ", ".join(f"{k.replace('_', ' ')} {counts.get(k, 0)}" for k in keys)


__all__ = ["DemoSeedError", "catalogue_size", "report", "seed_full_demo"]
