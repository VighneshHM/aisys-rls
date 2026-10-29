"""removing members

Revision ID: 0012
Revises: 0011

A member can be removed (`removed_at` is set): they keep their loans, fines and history but cannot borrow, their card is
switched off and they are hidden from the normal lists. These columns record who removed them, when and why, the status
they had, and when their personal details were erased. `status` itself keeps its two values (a database rule allows only
those) and is `blocked` while removed. Plain ADD COLUMN (SQLite supports it directly): other tables point at `members`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("members", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("members", sa.Column("removed_by", sa.String(64), nullable=False, server_default=""))
    op.add_column("members", sa.Column("removed_reason", sa.String(200), nullable=False, server_default=""))
    op.add_column("members", sa.Column("status_before_removal", sa.String(10), nullable=False, server_default=""))
    op.add_column("members", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("members") as batch:
        batch.drop_column("erased_at")
        batch.drop_column("status_before_removal")
        batch.drop_column("removed_reason")
        batch.drop_column("removed_by")
        batch.drop_column("removed_at")
