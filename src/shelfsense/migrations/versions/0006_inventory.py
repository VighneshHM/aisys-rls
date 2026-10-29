"""inventory: shelf-reading sessions and the tags each one saw

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("location", sa.String(100), nullable=False),
        sa.Column("handheld_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("started_by", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sweeps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("summary", sa.JSON, nullable=False),
        sa.CheckConstraint("status IN ('open','closed')", name="ck_inventory_sessions_status"),
    )
    op.create_index("ix_inventory_sessions_location", "inventory_sessions", ["location"])
    op.create_table(
        "inventory_scans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_id", sa.Integer, sa.ForeignKey("inventory_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tag_uid", sa.String(32), nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("signal", sa.String(8), nullable=False),
        sa.Column("home_location", sa.String(100), nullable=False, server_default=""),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("reads", sa.Integer, nullable=False, server_default="1"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_inventory_scans_session_id", "inventory_scans", ["session_id"])
    op.create_index("uq_inventory_scans_session_tag", "inventory_scans", ["session_id", "tag_uid"], unique=True)


def downgrade() -> None:
    op.drop_table("inventory_scans")
    op.drop_table("inventory_sessions")
