"""catalogue (bib records, items) and migration staging

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="staged"),
        sa.Column("mapping", sa.JSON, nullable=False),
        sa.Column("headers", sa.JSON, nullable=False),
        sa.Column("summary", sa.JSON, nullable=False),
        sa.Column("backup_path", sa.String(500), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('staged','validated','committed','rolled_back','failed')", name="ck_batches_status"
        ),
    )
    op.create_index("ix_migration_batches_file_sha256", "migration_batches", ["file_sha256"])
    op.create_table(
        "migration_rows",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("migration_batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_no", sa.Integer, nullable=False),
        sa.Column("raw", sa.JSON, nullable=False),
        sa.Column("mapped", sa.JSON, nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="staged"),
        sa.Column("errors", sa.JSON, nullable=False),
        sa.Column("duplicate_of", sa.String(80), nullable=False, server_default=""),
    )
    op.create_index("ix_migration_rows_batch_id", "migration_rows", ["batch_id"])
    op.create_index("ix_migration_rows_batch_status", "migration_rows", ["batch_id", "status"])

    op.create_table(
        "bib_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dedupe_key", sa.String(400), nullable=False, unique=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("author", sa.String(300), nullable=False, server_default=""),
        sa.Column("isbn", sa.String(13), nullable=True),
        sa.Column("publisher", sa.String(300), nullable=False, server_default=""),
        sa.Column("pub_year", sa.Integer, nullable=True),
        sa.Column("edition", sa.String(100), nullable=False, server_default=""),
        sa.Column("language", sa.String(50), nullable=False, server_default=""),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("migration_batches.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bib_records_title", "bib_records", ["title"])
    op.create_index("ix_bib_records_isbn", "bib_records", ["isbn"])
    op.create_index("ix_bib_records_batch_id", "bib_records", ["batch_id"])
    op.create_table(
        "items",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("bib_id", sa.Integer, sa.ForeignKey("bib_records.id"), nullable=False),
        sa.Column("accession_no", sa.String(32), nullable=False, unique=True),
        sa.Column("barcode", sa.String(64), nullable=True, unique=True),
        sa.Column("call_number", sa.String(100), nullable=False, server_default=""),
        sa.Column("location", sa.String(100), nullable=False, server_default=""),
        sa.Column("item_type", sa.String(16), nullable=False, server_default="book"),
        sa.Column("status", sa.String(16), nullable=False, server_default="available"),
        sa.Column("is_reference", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("price", sa.Numeric(10, 2), nullable=True),
        sa.Column("acquired_on", sa.Date, nullable=True),
        sa.Column("batch_id", sa.Integer, sa.ForeignKey("migration_batches.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_items_bib_id", "items", ["bib_id"])
    op.create_index("ix_items_batch_id", "items", ["batch_id"])


def downgrade() -> None:
    for table in ("items", "bib_records", "migration_rows", "migration_batches"):
        op.drop_table(table)
