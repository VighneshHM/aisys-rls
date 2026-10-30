"""fines: an append-only ledger of charges, payments and waivers

Existing stored balances become an 'opening' entry so every balance is explained by the ledger.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fine_entries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("member_id", sa.Integer, sa.ForeignKey("members.id"), nullable=False),
        sa.Column("loan_id", sa.Integer, sa.ForeignKey("loans.id"), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("actor", sa.String(64), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.CheckConstraint("kind IN ('opening','charge','payment','waiver')", name="ck_fine_entries_kind"),
        sa.CheckConstraint("amount_cents > 0", name="ck_fine_entries_amount"),
    )
    op.create_index("ix_fine_entries_member_id", "fine_entries", ["member_id"])
    op.create_index("ix_fine_entries_ts", "fine_entries", ["ts"])
    op.execute(
        "INSERT INTO fine_entries (ts, member_id, loan_id, kind, amount_cents, note, actor, detail) "
        "SELECT CURRENT_TIMESTAMP, id, NULL, 'opening', fines_cents, 'Opening balance', 'system', '{}' "
        "FROM members WHERE fines_cents > 0"
    )
    op.execute(
        "CREATE TRIGGER fine_entries_no_update BEFORE UPDATE ON fine_entries "
        "BEGIN SELECT RAISE(ABORT, 'fine_entries is append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER fine_entries_no_delete BEFORE DELETE ON fine_entries "
        "BEGIN SELECT RAISE(ABORT, 'fine_entries is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS fine_entries_no_update")
    op.execute("DROP TRIGGER IF EXISTS fine_entries_no_delete")
    op.drop_table("fine_entries")
