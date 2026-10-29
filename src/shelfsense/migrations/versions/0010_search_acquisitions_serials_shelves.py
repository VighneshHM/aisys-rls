"""full-text search index, shelves, acquisitions and serials

Revision ID: 0010
Revises: 0009

* `item_fts`: an SQLite FTS5 index with one row per item (the title and author of its bibliographic record plus the
  copy's own numbers and place). Triggers keep it current on every insert, edit and delete, so a new item is
  searchable the moment it is saved (real-time indexing). If this SQLite was built without FTS5 the index is simply
  not created and the catalogue falls back to plain matching.
* `shelves`: the shelf locations the library manages (capacity and a description).
* `purchase_orders` and `po_lines`: acquisition. Receiving a line creates the catalogue records and copies.
* `serial_titles` and `serial_issues`: subscriptions and the issues expected and received.
"""

# ruff: noqa: S608  (every SQL string here is built from constants in this file, never from input)
import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

FTS_COLUMNS = "rowid, title, author, isbn, publisher, accession_no, barcode, call_number, location"
SELECT_FOR = (
    "SELECT {id}, b.title, b.author, COALESCE(b.isbn, ''), b.publisher, {i}.accession_no, COALESCE({i}.barcode, ''), "
    "{i}.call_number, {i}.location FROM {src}"
)
TRIGGERS = {
    "trg_items_fts_ai": f"""
CREATE TRIGGER trg_items_fts_ai AFTER INSERT ON items BEGIN
  INSERT INTO item_fts({FTS_COLUMNS})
  {SELECT_FOR.format(id="NEW.id", i="NEW", src="bib_records b WHERE b.id = NEW.bib_id")};
END""",
    "trg_items_fts_au": f"""
CREATE TRIGGER trg_items_fts_au AFTER UPDATE OF accession_no, barcode, call_number, location, bib_id ON items BEGIN
  DELETE FROM item_fts WHERE rowid = OLD.id;
  INSERT INTO item_fts({FTS_COLUMNS})
  {SELECT_FOR.format(id="NEW.id", i="NEW", src="bib_records b WHERE b.id = NEW.bib_id")};
END""",
    "trg_items_fts_ad": """
CREATE TRIGGER trg_items_fts_ad AFTER DELETE ON items BEGIN
  DELETE FROM item_fts WHERE rowid = OLD.id;
END""",
    "trg_bibs_fts_au": f"""
CREATE TRIGGER trg_bibs_fts_au AFTER UPDATE OF title, author, isbn, publisher ON bib_records BEGIN
  DELETE FROM item_fts WHERE rowid IN (SELECT id FROM items WHERE bib_id = NEW.id);
  INSERT INTO item_fts({FTS_COLUMNS})
  SELECT i.id, NEW.title, NEW.author, COALESCE(NEW.isbn, ''), NEW.publisher, i.accession_no, COALESCE(i.barcode, ''),
         i.call_number, i.location FROM items i WHERE i.bib_id = NEW.id;
END""",
}


def _create_fts() -> None:
    conn = op.get_bind()
    try:
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE item_fts USING fts5("
            "title, author, isbn, publisher, accession_no, barcode, call_number, location, "
            "tokenize = 'unicode61 remove_diacritics 2')"
        )
    except OperationalError:  # this SQLite has no FTS5: the catalogue falls back to plain matching
        return
    for ddl in TRIGGERS.values():
        conn.exec_driver_sql(ddl)
    conn.exec_driver_sql(
        f"INSERT INTO item_fts({FTS_COLUMNS}) "
        + SELECT_FOR.format(id="i.id", i="i", src="items i JOIN bib_records b ON b.id = i.bib_id")
    )


def upgrade() -> None:
    _create_fts()

    op.create_table(
        "shelves",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(100), nullable=False, unique=True),  # equals items.location
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("capacity", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("capacity IS NULL OR capacity >= 0", name="ck_shelves_capacity"),
    )
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("po_no", sa.String(32), nullable=False, unique=True),
        sa.Column("supplier", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','ordered','part_received','received','cancelled')", name="ck_purchase_orders_status"
        ),
    )
    op.create_table(
        "po_lines",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("order_id", sa.Integer, sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("author", sa.String(300), nullable=False, server_default=""),
        sa.Column("isbn", sa.String(13), nullable=True),
        sa.Column("publisher", sa.String(300), nullable=False, server_default=""),
        sa.Column("pub_year", sa.Integer, nullable=True),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_price_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("location", sa.String(100), nullable=False, server_default=""),
        sa.Column("received", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bib_id", sa.Integer, sa.ForeignKey("bib_records.id"), nullable=True),
        sa.CheckConstraint("quantity > 0 AND received >= 0 AND received <= quantity", name="ck_po_lines_quantity"),
    )
    op.create_index("ix_po_lines_order_id", "po_lines", ["order_id"])
    op.create_table(
        "serial_titles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("issn", sa.String(9), nullable=False, server_default=""),
        sa.Column("frequency", sa.String(12), nullable=False),
        sa.Column("supplier", sa.String(200), nullable=False, server_default=""),
        sa.Column("location", sa.String(100), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("bib_id", sa.Integer, sa.ForeignKey("bib_records.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("frequency IN ('weekly','monthly','quarterly','yearly')", name="ck_serial_titles_frequency"),
    )
    op.create_table(
        "serial_issues",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("serial_id", sa.Integer, sa.ForeignKey("serial_titles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("expected_on", sa.Date, nullable=False),
        sa.Column("received_on", sa.Date, nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="expected"),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=True),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.CheckConstraint("status IN ('expected','received','claimed','missing')", name="ck_serial_issues_status"),
    )
    op.create_index("uq_serial_issues_label", "serial_issues", ["serial_id", "label"], unique=True)
    op.create_index("ix_serial_issues_status", "serial_issues", ["status"])


def downgrade() -> None:
    for table in ("serial_issues", "serial_titles", "po_lines", "purchase_orders", "shelves"):
        op.drop_table(table)
    conn = op.get_bind()
    for name in TRIGGERS:
        conn.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    conn.exec_driver_sql("DROP TABLE IF EXISTS item_fts")
