"""ORM models for the foundation layer: identity, RBAC, sessions, audit."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Table, text
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
