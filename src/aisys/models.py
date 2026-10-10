"""ORM models for the foundation layer: identity, RBAC, sessions, audit."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aisys.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(200), default="")


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    permissions: Mapped[list[Permission]] = relationship(secondary=role_permissions, lazy="selectin")


class User(Base):
    """A staff account. Patrons are NOT users: they cannot authenticate."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    role: Mapped[Role] = relationship(lazy="joined")


class Card(Base):
    """A smart card. kind='staff' may log in; kind='patron' only identifies a member."""

    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(10))  # 'staff' | 'patron'
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    member_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # member no. in the ILMS
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User | None] = relationship(lazy="joined")


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    method: Mapped[str] = mapped_column(String(10))  # 'card' | 'password'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(lazy="joined")


class AuditEvent(Base):
    """Append-only (enforced by DB triggers, see migration 0001)."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)  # username, 'system' or 'anonymous'
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(10), default="success")  # success | denied | failure
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")


# ---------------------------------------------------------------- catalogue (reference mini-ILMS)
class MigrationBatch(Base):
    """One uploaded spreadsheet. Moves staged -> validated -> committed -> rolled_back (or failed)."""

    __tablename__ = "migration_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="staged")
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)  # canonical field -> source header
    headers: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)  # validation + reconciliation numbers
    backup_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MigrationRow(Base):
    """Staging area: every source row is kept verbatim, with its validation outcome."""

    __tablename__ = "migration_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("migration_batches.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)  # row number as shown in Excel (header row = first used row)
    raw: Mapped[dict] = mapped_column(JSON)
    mapped: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="staged")  # valid|invalid|duplicate|migrated
    errors: Mapped[list] = mapped_column(JSON, default=list)  # [{field, code, message}]
    duplicate_of: Mapped[str] = mapped_column(String(80), default="")


class BibRecord(Base):
    __tablename__ = "bib_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(400), unique=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    author: Mapped[str] = mapped_column(String(300), default="")
    isbn: Mapped[str | None] = mapped_column(String(13), nullable=True, index=True)
    publisher: Mapped[str] = mapped_column(String(300), default="")
    pub_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    edition: Mapped[str] = mapped_column(String(100), default="")
    language: Mapped[str] = mapped_column(String(50), default="")
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("migration_batches.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Item(Base):
    """A physical copy. Existing items are never updated by migration; only inserted."""

    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    bib_id: Mapped[int] = mapped_column(ForeignKey("bib_records.id"), index=True)
    accession_no: Mapped[str] = mapped_column(String(32), unique=True)
    barcode: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    call_number: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(100), default="")
    item_type: Mapped[str] = mapped_column(String(16), default="book")
    status: Mapped[str] = mapped_column(String(16), default="available")
    is_reference: Mapped[bool] = mapped_column(Boolean, default=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    acquired_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("migration_batches.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    bib: Mapped[BibRecord] = relationship(lazy="joined")


# ---------------------------------------------------------------- RFID tags
class RfidTag(Base):
    """One link between a physical tag and an item (one interval in the tag's life).

    status: 'active'  = currently on the item
            'removed' = taken off the item; the tag can be linked again (to any item)
            'retired' = taken off and out of service for good (damaged / lost); the UID can never be linked again
    A tag UID has at most one active link and an item has at most one active tag (both enforced by the database).
    """

    __tablename__ = "rfid_tags"
    __table_args__ = (
        Index("uq_rfid_tags_active_item", "item_id", unique=True, sqlite_where=text("status = 'active'")),
        Index("uq_rfid_tags_active_uid", "tag_uid", unique=True, sqlite_where=text("status = 'active'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    tag_uid: Mapped[str] = mapped_column(String(32), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | removed | retired
    security_armed: Mapped[bool] = mapped_column(Boolean, default=True)
    reader_id: Mapped[str] = mapped_column(String(64), default="")
    tagged_by: Mapped[str] = mapped_column(String(64))
    tagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str] = mapped_column(String(200), default="")

    item: Mapped[Item] = relationship(lazy="joined")


class TagEvent(Base):
    """Device/tag activity stream for monitoring (separate from the audit log, which records staff actions)."""

    __tablename__ = "tag_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    tag_uid: Mapped[str] = mapped_column(String(32), index=True)
    reader_id: Mapped[str] = mapped_column(String(64), default="")
    event_type: Mapped[str] = mapped_column(
        String(20), index=True
    )  # read|tagged|retagged|removed|retired|encode_failed
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- circulation
class Member(Base):
    """A library member. Patrons are identified by their card but are never users: they cannot log in."""

    __tablename__ = "members"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_no: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | blocked (see removed_at)
    block_reason: Mapped[str] = mapped_column(String(200), default="")
    fines_cents: Mapped[int] = mapped_column(Integer, default=0)
    email: Mapped[str] = mapped_column(String(200), default="")  # where reminders go (optional)
    phone: Mapped[str] = mapped_column(String(32), default="")
    reminders_on: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # A removed member (removed_at set) keeps their history (loans, fines, events) but cannot borrow: their status is
    # 'blocked' underneath, so every older code path refuses them too. The database rule allows only active|blocked, and
    # rebuilding this table (other tables point at it) is not worth the risk. Erasing clears the personal details.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[str] = mapped_column(String(64), default="")
    removed_reason: Mapped[str] = mapped_column(String(200), default="")
    status_before_removal: Mapped[str] = mapped_column(String(10), default="")
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Loan(Base):
    """One borrowing of one item. Open while returned_at is empty; an item has at most one open loan (DB-enforced)."""

    __tablename__ = "loans"
    __table_args__ = (Index("uq_loans_open_item", "item_id", unique=True, sqlite_where=text("returned_at IS NULL")),)
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    out_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    renewals: Mapped[int] = mapped_column(Integer, default=0)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    out_by: Mapped[str] = mapped_column(String(64))
    returned_by: Mapped[str] = mapped_column(String(64), default="")

    member: Mapped[Member] = relationship(lazy="joined")
    item: Mapped[Item] = relationship(lazy="joined")


class CircEvent(Base):
    """Auditable transaction history: every circulation attempt, including refusals and ILMS failures.

    Append-only (DB triggers, migration 0005). `txn_id` is the id sent to the ILMS, so a local row can be matched to
    the ILMS's own log.
    """

    __tablename__ = "circ_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)  # checkout | renew | checkin | due_date
    outcome: Mapped[str] = mapped_column(String(10))  # ok | refused | failed
    code: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(String(300), default="")
    protocol: Mapped[str] = mapped_column(String(10), default="")
    txn_id: Mapped[str] = mapped_column(String(40), index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), nullable=True, index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True, index=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- inventory (shelf reading)
class InventorySession(Base):
    """One shelf-reading run: a location, a handheld, and the tags it has seen so far."""

    __tablename__ = "inventory_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    location: Mapped[str] = mapped_column(String(100), index=True)
    handheld_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | closed
    started_by: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_by: Mapped[str] = mapped_column(String(64), default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sweeps: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)  # final counts, frozen when the session is closed


class InventoryScan(Base):
    """A distinct tag seen in a session, how it was classified, and the beep/flash it earned."""

    __tablename__ = "inventory_scans"
    __table_args__ = (Index("uq_inventory_scans_session_tag", "session_id", "tag_uid", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("inventory_sessions.id", ondelete="CASCADE"), index=True)
    tag_uid: Mapped[str] = mapped_column(String(32))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    result: Mapped[str] = mapped_column(String(16))  # found | shelved_on_loan | misplaced | unknown
    signal: Mapped[str] = mapped_column(String(8))  # ok | warning | error: what the handheld beeped and flashed
    home_location: Mapped[str] = mapped_column(String(100), default="")  # where the item belongs
    note: Mapped[str] = mapped_column(String(200), default="")
    reads: Mapped[int] = mapped_column(Integer, default=1)  # times the tag was read (duplicates are normal)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FineEntry(Base):
    """Fine ledger, append-only (DB triggers, migration 0007). Balance = opening + charges - payments - waivers."""

    __tablename__ = "fine_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(10))  # opening | charge | payment | waiver
    amount_cents: Mapped[int] = mapped_column(Integer)  # always positive; the kind says which way it moves the balance
    note: Mapped[str] = mapped_column(String(300), default="")
    actor: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- security gate, CCTV, notifications
class GatePassage(Base):
    """One walk through a gate (append-only). Footfall is the sum of `people`."""

    __tablename__ = "gate_passages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    gate_id: Mapped[str] = mapped_column(String(64))
    people: Mapped[int] = mapped_column(Integer, default=1)


class GateEvent(Base):
    """One tag seen at a gate and what we made of it (append-only, DB triggers, migration 0008).

    kind: unauthorised   security bit on, the item is not on loan: someone is taking it out (ALARM)
          armed_on_loan  security bit on although the item is on loan: a check-out that could not switch it off (ALARM)
          unissued       a tag that belongs to no item (ALARM if its bit is on)
          authorised     bit off and the item is on loan: a normal exit
          bit_off        bit off but the item is not on loan: it was switched off without a loan (warning)
    The alarm is decided from the security bit read at the gate, never from the library system.
    """

    __tablename__ = "gate_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    passage_id: Mapped[int] = mapped_column(ForeignKey("gate_passages.id"), index=True)
    gate_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    alarm: Mapped[bool] = mapped_column(Boolean)
    tag_uid: Mapped[str] = mapped_column(String(32), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True, index=True)
    accession_no: Mapped[str] = mapped_column(String(32), default="")  # copied, so the record survives catalogue edits
    title: Mapped[str] = mapped_column(String(500), default="")
    loan_member_no: Mapped[str] = mapped_column(String(32), default="")
    security_armed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    on_loan: Mapped[bool] = mapped_column(Boolean, default=False)
    people: Mapped[int] = mapped_column(Integer, default=1)
    decided_by: Mapped[str] = mapped_column(String(20), default="security_bit")
    duplicate_of: Mapped[int | None] = mapped_column(Integer, nullable=True)  # an earlier alarm for the same tag
    camera_id: Mapped[str] = mapped_column(String(64), default="")
    image_error: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(String(300), default="")


class GateImage(Base):
    """The CCTV snapshot taken for an alarm (append-only)."""

    __tablename__ = "gate_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("gate_events.id"), unique=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    camera_id: Mapped[str] = mapped_column(String(64), default="")
    content_type: Mapped[str] = mapped_column(String(40))
    data: Mapped[bytes] = mapped_column(LargeBinary)


class NotificationPref(Base):
    """Which channels a staff user wants alerts on, and where to reach them (email address, phone, printer)."""

    __tablename__ = "notification_prefs"
    __table_args__ = (Index("uq_notification_prefs_user_channel", "user_id", "channel", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(String(10))  # email | sms | print
    address: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Notification(Base):
    """A message waiting to go out (or already sent / given up on). Delivery is a separate step with retries."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "uq_notifications_reminder", "loan_id", "kind", "channel", "due_on", unique=True,
            sqlite_where=text("loan_id IS NOT NULL"),
        ),
    )  # fmt: skip
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    channel: Mapped[str] = mapped_column(String(10))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    recipient: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="queued", index=True)  # queued | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(300), default="")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_ref: Mapped[str] = mapped_column(String(100), default="")
    event_id: Mapped[int | None] = mapped_column(ForeignKey("gate_events.id"), nullable=True, index=True)
    # patron reminders: which loan and due date a reminder is about (a reminder is sent once per loan, kind, due date)
    kind: Mapped[str] = mapped_column(String(20), default="")  # "" = staff alert | due_soon | due_today
    member_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # members.id
    loan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # loans.id
    due_on: Mapped[str] = mapped_column(String(10), default="")


# ---------------------------------------------------------------- shelves, acquisitions, serials
class Shelf(Base):
    """A shelf location the library manages. `code` equals the `location` text on its items."""

    __tablename__ = "shelves"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PurchaseOrder(Base):
    """An order to a supplier. draft -> ordered -> part_received -> received (or cancelled)."""

    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_no: Mapped[str] = mapped_column(String(32), unique=True)
    supplier: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="draft")
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list[PoLine]] = relationship(order_by="PoLine.id", lazy="selectin", cascade="all, delete-orphan")


class PoLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(300), default="")
    isbn: Mapped[str | None] = mapped_column(String(13), nullable=True)
    publisher: Mapped[str] = mapped_column(String(300), default="")
    pub_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    location: Mapped[str] = mapped_column(String(100), default="")
    received: Mapped[int] = mapped_column(Integer, default=0)
    bib_id: Mapped[int | None] = mapped_column(ForeignKey("bib_records.id"), nullable=True)


class SerialTitle(Base):
    """A subscription: a journal or magazine that arrives on a schedule."""

    __tablename__ = "serial_titles"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    issn: Mapped[str] = mapped_column(String(9), default="")
    frequency: Mapped[str] = mapped_column(String(12))  # weekly | monthly | quarterly | yearly
    supplier: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    bib_id: Mapped[int | None] = mapped_column(ForeignKey("bib_records.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    issues: Mapped[list[SerialIssue]] = relationship(
        order_by="SerialIssue.expected_on", lazy="selectin", cascade="all, delete-orphan"
    )


class SerialIssue(Base):
    __tablename__ = "serial_issues"
    __table_args__ = (Index("uq_serial_issues_label", "serial_id", "label", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    serial_id: Mapped[int] = mapped_column(ForeignKey("serial_titles.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(40))
    expected_on: Mapped[date] = mapped_column(Date)
    received_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="expected", index=True)  # expected|received|claimed|missing
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")


# ---------------------------------------------------------------- settings changed in the browser
class AppSetting(Base):
    """A library rule an administrator changed on the Settings page. It overrides the same value in the TOML file."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(String(40))
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
