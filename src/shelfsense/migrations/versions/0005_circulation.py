"""circulation: members, loans, and the append-only circulation event history

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "members",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("member_no", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("block_reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("fines_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active','blocked')", name="ck_members_status"),
        sa.CheckConstraint("fines_cents >= 0", name="ck_members_fines"),
    )
    op.create_index("ix_members_name", "members", ["name"])

    op.create_table(
        "loans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("member_id", sa.Integer, sa.ForeignKey("members.id"), nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=False),
        sa.Column("out_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewals", sa.Integer, nullable=False, server_default="0"),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("out_by", sa.String(64), nullable=False),
        sa.Column("returned_by", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_loans_member_id", "loans", ["member_id"])
    op.create_index("ix_loans_item_id", "loans", ["item_id"])
    op.create_index(
        "uq_loans_open_item", "loans", ["item_id"], unique=True, sqlite_where=sa.text("returned_at IS NULL")
    )

    op.create_table(
        "circ_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("code", sa.String(32), nullable=False, server_default=""),
        sa.Column("message", sa.String(300), nullable=False, server_default=""),
        sa.Column("protocol", sa.String(10), nullable=False, server_default=""),
        sa.Column("txn_id", sa.String(40), nullable=False),
        sa.Column("member_id", sa.Integer, sa.ForeignKey("members.id"), nullable=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=True),
        sa.Column("loan_id", sa.Integer, sa.ForeignKey("loans.id"), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON, nullable=False),
    )
    for col in ("ts", "action", "txn_id", "member_id", "item_id"):
        op.create_index(f"ix_circ_events_{col}", "circ_events", [col])
    op.execute(
        "CREATE TRIGGER circ_events_no_update BEFORE UPDATE ON circ_events "
        "BEGIN SELECT RAISE(ABORT, 'circ_events is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER circ_events_no_delete BEFORE DELETE ON circ_events "
        "BEGIN SELECT RAISE(ABORT, 'circ_events is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS circ_events_no_update")
    op.execute("DROP TRIGGER IF EXISTS circ_events_no_delete")
    op.drop_table("circ_events")
    op.drop_table("loans")
    op.drop_table("members")
