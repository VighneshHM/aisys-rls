"""tag lifecycle: one row per tag-to-item link; states active / removed / retired

Before: one row per tag UID (UNIQUE), states active | retired, so a tag could never be linked again.
After:  one row per link (the UID may appear many times), states active | removed | retired.
        Partial unique indexes keep "one active tag per item" and add "one active item per tag".
SQLite cannot drop an unnamed UNIQUE constraint in place, so the table is rebuilt.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _create_table(name: str, *, new: bool) -> None:
    cols = [
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tag_uid", sa.String(32), nullable=False, unique=not new),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("security_armed", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("reader_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("tagged_by", sa.String(64), nullable=False),
        sa.Column("tagged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at" if new else "retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason" if new else "retired_reason", sa.String(200), nullable=False, server_default=""),
    ]
    allowed = "'active','removed','retired'" if new else "'active','retired'"
    op.create_table(name, *cols, sa.CheckConstraint(f"status IN ({allowed})", name="ck_rfid_tags_status"))


def upgrade() -> None:
    op.drop_index("uq_rfid_tags_active_item", table_name="rfid_tags")
    op.drop_index("ix_rfid_tags_item_id", table_name="rfid_tags")
    op.rename_table("rfid_tags", "rfid_tags_old")
    _create_table("rfid_tags", new=True)
    op.execute(
        "INSERT INTO rfid_tags (id, tag_uid, item_id, status, security_armed, reader_id, tagged_by, tagged_at, "
        "ended_at, end_reason) SELECT id, tag_uid, item_id, status, security_armed, reader_id, tagged_by, tagged_at, "
        "retired_at, retired_reason FROM rfid_tags_old"
    )
    # note: earlier 'retired' rows stay retired. They were promised "never reused", so we keep that promise.
    op.drop_table("rfid_tags_old")
    op.create_index("ix_rfid_tags_tag_uid", "rfid_tags", ["tag_uid"])
    op.create_index("ix_rfid_tags_item_id", "rfid_tags", ["item_id"])
    op.create_index(
        "uq_rfid_tags_active_item", "rfid_tags", ["item_id"], unique=True, sqlite_where=sa.text("status = 'active'")
    )
    op.create_index(
        "uq_rfid_tags_active_uid", "rfid_tags", ["tag_uid"], unique=True, sqlite_where=sa.text("status = 'active'")
    )


def downgrade() -> None:
    """Lossy by nature: the old shape holds ONE row per tag UID, so only each tag's latest link is kept."""
    op.drop_index("uq_rfid_tags_active_uid", table_name="rfid_tags")
    op.drop_index("uq_rfid_tags_active_item", table_name="rfid_tags")
    op.drop_index("ix_rfid_tags_item_id", table_name="rfid_tags")
    op.drop_index("ix_rfid_tags_tag_uid", table_name="rfid_tags")
    op.rename_table("rfid_tags", "rfid_tags_new")
    _create_table("rfid_tags", new=False)
    op.execute(
        "INSERT INTO rfid_tags (id, tag_uid, item_id, status, security_armed, reader_id, tagged_by, tagged_at, "
        "retired_at, retired_reason) SELECT id, tag_uid, item_id, "
        "CASE status WHEN 'active' THEN 'active' ELSE 'retired' END, security_armed, reader_id, tagged_by, tagged_at, "
        "ended_at, end_reason FROM rfid_tags_new WHERE id IN (SELECT MAX(id) FROM rfid_tags_new GROUP BY tag_uid)"
    )
    op.drop_table("rfid_tags_new")
    op.create_index("ix_rfid_tags_item_id", "rfid_tags", ["item_id"])
    op.create_index(
        "uq_rfid_tags_active_item", "rfid_tags", ["item_id"], unique=True, sqlite_where=sa.text("status = 'active'")
    )
