"""patron contact details and loan reminders

Revision ID: 0009
Revises: 0008

Members get an optional e-mail address, phone number and an on/off switch for reminders. A notification can now
say which loan and due date it is about; a partial unique index makes each reminder (loan, kind, channel, due
date) exist at most once, so repeated scans, restarts or two schedulers can never send the same reminder twice.
`member_id` and `loan_id` are plain numbers: SQLite cannot add a foreign key to an existing table without
rebuilding it, and rebuilding `members` (which other tables point at) is not worth the risk.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # a reminder for a loan that was returned or renewed before it went out is "cancelled": widen the status check.
    # Nothing points at `notifications`, so rebuilding it is safe (unlike `members`, below).
    with op.batch_alter_table("notifications", recreate="always") as batch:
        batch.drop_constraint("ck_notifications_status", type_="check")
        batch.create_check_constraint("ck_notifications_status", "status IN ('queued','sent','failed','cancelled')")
    # plain ADD COLUMN (SQLite supports it directly): rebuilding `members` would drop a table other tables point at
    op.add_column("members", sa.Column("email", sa.String(200), nullable=False, server_default=""))
    op.add_column("members", sa.Column("phone", sa.String(32), nullable=False, server_default=""))
    op.add_column("members", sa.Column("reminders_on", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("notifications", sa.Column("kind", sa.String(20), nullable=False, server_default=""))
    op.add_column("notifications", sa.Column("member_id", sa.Integer, nullable=True))
    op.add_column("notifications", sa.Column("loan_id", sa.Integer, nullable=True))
    op.add_column("notifications", sa.Column("due_on", sa.String(10), nullable=False, server_default=""))
    op.create_index("ix_notifications_member_id", "notifications", ["member_id"])
    op.create_index("ix_notifications_loan_id", "notifications", ["loan_id"])
    op.create_index(
        "uq_notifications_reminder",
        "notifications",
        ["loan_id", "kind", "channel", "due_on"],
        unique=True,
        sqlite_where=sa.text("loan_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_notifications_reminder", table_name="notifications")
    op.drop_index("ix_notifications_loan_id", table_name="notifications")
    op.drop_index("ix_notifications_member_id", table_name="notifications")
    op.execute("UPDATE notifications SET status = 'failed' WHERE status = 'cancelled'")
    with op.batch_alter_table("notifications", recreate="always") as batch:
        batch.drop_constraint("ck_notifications_status", type_="check")
        batch.create_check_constraint("ck_notifications_status", "status IN ('queued','sent','failed')")
        for col in ("due_on", "loan_id", "member_id", "kind"):
            batch.drop_column(col)
    with op.batch_alter_table("members") as batch:
        for col in ("reminders_on", "phone", "email"):
            batch.drop_column(col)
