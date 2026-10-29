"""settings changed on the Settings page

Revision ID: 0011
Revises: 0010

One row per library rule (loan days, fine rates, reminder days, sign-in limits...) that an administrator changed in
the browser. A row overrides the same value from the TOML file; deleting the row goes back to the file's value.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", sa.String(40), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
