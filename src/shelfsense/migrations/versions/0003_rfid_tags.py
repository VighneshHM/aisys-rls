"""RFID tags bound to items, and the tag event stream

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rfid_tags",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tag_uid", sa.String(32), nullable=False, unique=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("security_armed", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("reader_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("tagged_by", sa.String(64), nullable=False),
        sa.Column("tagged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_reason", sa.String(200), nullable=False, server_default=""),
        sa.CheckConstraint("status IN ('active','retired')", name="ck_rfid_tags_status"),
    )
    op.create_index("ix_rfid_tags_item_id", "rfid_tags", ["item_id"])
    # at most one ACTIVE tag per item, enforced by the database (not just application code)
    op.create_index(
        "uq_rfid_tags_active_item", "rfid_tags", ["item_id"], unique=True, sqlite_where=sa.text("status = 'active'")
    )
    op.create_table(
        "tag_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tag_uid", sa.String(32), nullable=False),
        sa.Column("reader_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON, nullable=False),
    )
    op.create_index("ix_tag_events_ts", "tag_events", ["ts"])
    op.create_index("ix_tag_events_tag_uid", "tag_events", ["tag_uid"])
    op.create_index("ix_tag_events_event_type", "tag_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("tag_events")
    op.drop_table("rfid_tags")
