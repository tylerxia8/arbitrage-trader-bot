"""Initial evidence schema: raw archive, markets, terms, registry, audit log

Revision ID: 0001
Revises:
Create Date: 2026-08-12

Creates the Milestone 0 foundation. The append-only guarantee on
``raw_message`` and ``audit_event`` is enforced by database triggers rather
than by convention: application code that accidentally updates an archived
payload would invalidate every replay derived from it, and "we agreed not to"
is not a control.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

APPEND_ONLY_TABLES = ("raw_message", "audit_event")

_GUARD_FN = """
CREATE OR REPLACE FUNCTION arbbot_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'table % is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "raw_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("source_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_message")),
        sa.UniqueConstraint("venue", "channel", "sha256", name="uq_raw_message_dedupe"),
    )
    op.create_index("ix_raw_message_channel_received", "raw_message", ["channel", "received_ts"])
    op.create_index(
        "ix_raw_message_venue_sequence", "raw_message", ["venue", "channel", "sequence"]
    )

    op.create_table(
        "market",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("ticker", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("close_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terms_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "first_seen_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market")),
        sa.UniqueConstraint("venue", "ticker", name="uq_market_venue_ticker"),
    )
    op.create_index("ix_market_event", "market", ["venue", "event_id"])

    op.create_table(
        "terms_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("market_id", sa.Uuid(), nullable=False),
        sa.Column(
            "fetched_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("raw_terms", sa.Text(), nullable=False),
        sa.Column("normalized_terms", JSON_TYPE, nullable=False),
        sa.Column("terms_hash", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["market_id"],
            ["market.id"],
            name=op.f("fk_terms_version_market_id_market"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_terms_version")),
        sa.UniqueConstraint("market_id", "terms_hash", name="uq_terms_version_market_hash"),
    )
    op.create_index("ix_terms_version_market_fetched", "terms_version", ["market_id", "fetched_ts"])

    op.create_table(
        "relationship",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "relationship_type",
            sa.Enum(
                "EXHAUSTIVE_BASKET",
                "IMPLICATION_PAIR",
                "INTERVAL_PARTITION",
                name="relationship_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "SUSPENDED",
                "RETIRED",
                name="relationship_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("legs", JSON_TYPE, nullable=False),
        sa.Column("payout_proof", JSON_TYPE, nullable=False),
        sa.Column("dependency_hashes", JSON_TYPE, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("review_due_ts", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name=op.f("ck_relationship_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship")),
        sa.UniqueConstraint("slug", "version", name="uq_relationship_slug_version"),
    )
    op.create_index("ix_relationship_status", "relationship", ["status"])

    op.create_table(
        "approval",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("relationship_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column(
            "decision",
            sa.Enum("APPROVED", "REJECTED", name="approval_decision", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "decided_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("scope", JSON_TYPE, nullable=False),
        sa.ForeignKeyConstraint(
            ["relationship_id"],
            ["relationship.id"],
            name=op.f("fk_approval_relationship_id_relationship"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval")),
    )
    op.create_index("ix_approval_relationship", "approval", ["relationship_id", "decided_ts"])

    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "occurred_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("detail", JSON_TYPE, nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("is_privileged", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index("ix_audit_event_subject", "audit_event", ["subject_type", "subject_id"])
    op.create_index("ix_audit_event_occurred", "audit_event", ["occurred_ts"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute(_GUARD_FN)
        for table in APPEND_ONLY_TABLES:
            op.execute(
                # Table names come from the module-level tuple above, never
                # from input; there is no injection surface here.
                f"CREATE TRIGGER {table}_append_only "
                f"BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION arbbot_reject_mutation();"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
        op.execute("DROP FUNCTION IF EXISTS arbbot_reject_mutation();")

    op.drop_table("audit_event")
    op.drop_table("approval")
    op.drop_table("relationship")
    op.drop_table("terms_version")
    op.drop_table("market")
    op.drop_table("raw_message")
