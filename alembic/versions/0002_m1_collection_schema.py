"""M1 collection: subscription keys, book snapshots, feed health

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-12

Three changes, one of them a correction.

**Deduplication keyed on stream identity, not content.** Milestone 0 made
``(venue, channel, sha256)`` unique, on the reasoning that reconnects replay
messages and the archive must not double-count them. That is wrong in a way
that loses data: two heartbeats a minute apart are byte-identical and both
real, and the constraint would silently reject the second. Identity is the
venue's own statement of "this is message N of this subscription", so the
constraint becomes ``(venue, subscription_key, sequence)``. Rows with a NULL
sequence never collide -- NULL is distinct from NULL in a unique index -- so
unsequenced messages are always stored. The content hash remains, as an
integrity fingerprint rather than an identity.

``subscription_key`` is needed because sequence numbers are per-subscription:
without it, message 5 of one market collides with message 5 of another.

**book_snapshot** and **feed_health** are derived artifacts, deliberately not
append-only. Snapshots can be pruned and rebuilt by replaying the archive;
only the archive itself is evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "book_snapshot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("ticker", sa.String(length=128), nullable=False),
        sa.Column(
            "captured_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("yes_levels", JSON_TYPE, nullable=False),
        sa.Column("no_levels", JSON_TYPE, nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("raw_message_id", sa.BigInteger(), nullable=True),
        # RESTRICT: deleting archived evidence a snapshot derives from must
        # fail loudly rather than quietly orphan the derivation.
        sa.ForeignKeyConstraint(
            ["raw_message_id"],
            ["raw_message.id"],
            name=op.f("fk_book_snapshot_raw_message_id_raw_message"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_book_snapshot")),
    )
    op.create_index(
        "ix_book_snapshot_market_captured", "book_snapshot", ["venue", "ticker", "captured_ts"]
    )
    op.create_index("ix_book_snapshot_sequence", "book_snapshot", ["venue", "ticker", "sequence"])

    op.create_table(
        "feed_health",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "observed_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("subscription_key", sa.String(length=160), nullable=False),
        sa.Column("messages", sa.BigInteger(), nullable=False),
        sa.Column("gaps", sa.Integer(), nullable=False),
        sa.Column("missing_messages", sa.BigInteger(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("rewinds", sa.Integer(), nullable=False),
        sa.Column("reconnects", sa.Integer(), nullable=False),
        sa.Column("parse_errors", sa.Integer(), nullable=False),
        sa.Column("last_message_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lag_ms", sa.Integer(), nullable=True),
        sa.Column("is_healthy", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feed_health")),
    )
    op.create_index(
        "ix_feed_health_stream_observed",
        "feed_health",
        ["venue", "subscription_key", "observed_ts"],
    )

    op.add_column(
        "raw_message", sa.Column("subscription_key", sa.String(length=160), nullable=True)
    )
    op.drop_constraint(op.f("uq_raw_message_dedupe"), "raw_message", type_="unique")
    op.drop_index(op.f("ix_raw_message_venue_sequence"), table_name="raw_message")
    op.create_index(
        "ix_raw_message_venue_sequence",
        "raw_message",
        ["venue", "subscription_key", "sequence"],
    )
    op.create_unique_constraint(
        "uq_raw_message_stream_sequence",
        "raw_message",
        ["venue", "subscription_key", "sequence"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_raw_message_stream_sequence", "raw_message", type_="unique")
    op.drop_index("ix_raw_message_venue_sequence", table_name="raw_message")
    op.create_index(
        op.f("ix_raw_message_venue_sequence"), "raw_message", ["venue", "channel", "sequence"]
    )
    op.create_unique_constraint(
        op.f("uq_raw_message_dedupe"), "raw_message", ["venue", "channel", "sha256"]
    )
    op.drop_column("raw_message", "subscription_key")

    op.drop_index("ix_feed_health_stream_observed", table_name="feed_health")
    op.drop_table("feed_health")
    op.drop_index("ix_book_snapshot_sequence", table_name="book_snapshot")
    op.drop_index("ix_book_snapshot_market_captured", table_name="book_snapshot")
    op.drop_table("book_snapshot")
