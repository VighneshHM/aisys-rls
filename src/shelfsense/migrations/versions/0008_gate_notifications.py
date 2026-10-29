"""gate events, CCTV snapshots, and the notification queue with per-user preferences

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APPEND_ONLY = ("gate_passages", "gate_events", "gate_images")


def upgrade() -> None:
    op.create_table(
        "gate_passages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gate_id", sa.String(64), nullable=False),
        sa.Column("people", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_index("ix_gate_passages_ts", "gate_passages", ["ts"])
    op.create_table(
        "gate_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("passage_id", sa.Integer, sa.ForeignKey("gate_passages.id"), nullable=False),
        sa.Column("gate_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("alarm", sa.Boolean, nullable=False),
        sa.Column("tag_uid", sa.String(32), nullable=False),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("items.id"), nullable=True),
        sa.Column("accession_no", sa.String(32), nullable=False, server_default=""),
        sa.Column("title", sa.String(500), nullable=False, server_default=""),
        sa.Column("loan_member_no", sa.String(32), nullable=False, server_default=""),
        sa.Column("security_armed", sa.Boolean, nullable=True),
        sa.Column("on_loan", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("people", sa.Integer, nullable=False, server_default="1"),
        sa.Column("decided_by", sa.String(20), nullable=False, server_default="security_bit"),
        sa.Column("duplicate_of", sa.Integer, nullable=True),
        sa.Column("camera_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("image_error", sa.String(200), nullable=False, server_default=""),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.CheckConstraint(
            "kind IN ('unauthorised','armed_on_loan','unissued','authorised','bit_off')", name="ck_gate_events_kind"
        ),
    )
    for col in ("ts", "gate_id", "kind", "tag_uid", "item_id", "passage_id"):
        op.create_index(f"ix_gate_events_{col}", "gate_events", [col])
    op.create_table(
        "gate_images",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("event_id", sa.Integer, sa.ForeignKey("gate_events.id"), nullable=False, unique=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("camera_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("content_type", sa.String(40), nullable=False),
        sa.Column("data", sa.LargeBinary, nullable=False),
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )

    op.create_table(
        "notification_prefs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("address", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("channel IN ('email','sms','print')", name="ck_notification_prefs_channel"),
    )
    op.create_index("uq_notification_prefs_user_channel", "notification_prefs", ["user_id", "channel"], unique=True)
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("recipient", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(300), nullable=False, server_default=""),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_ref", sa.String(100), nullable=False, server_default=""),
        sa.Column("event_id", sa.Integer, sa.ForeignKey("gate_events.id"), nullable=True),
        sa.CheckConstraint("channel IN ('email','sms','print')", name="ck_notifications_channel"),
        sa.CheckConstraint("status IN ('queued','sent','failed')", name="ck_notifications_status"),
    )
    op.create_index("ix_notifications_status", "notifications", ["status"])
    op.create_index("ix_notifications_event_id", "notifications", ["event_id"])


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("notification_prefs")
    for table in reversed(APPEND_ONLY):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
        op.drop_table(table)
