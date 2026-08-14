"""Record which markets each poll cycle confirmed

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-14

``book_snapshot`` records when a book *changed*, not when it was *observed*.
Unchanged polls are deliberately not re-archived, which is right for storage
and was silently wrong for analysis: reading ``captured_ts`` as the quote's age
charges a leg for every second since it last moved, even while a poller was
confirming it current every second.

The fast-poll probe made the size of that distortion measurable. Across six
legs of a live temperature partition polled every second, the median gap
between changes was three to four seconds and the longest ran past twelve
minutes -- so a two-second freshness gate rejected as stale almost everything
that had in fact just been confirmed. Every "the edge was gone before we could
see it" result produced before this table is uninterpretable.

One row per cycle rather than per market: a cycle is the unit that confirms,
the tickers ride along in an array, and a seven-day run costs thousands of rows
rather than hundreds of thousands.

Append-only. A mutable "last confirmed" column would be smaller and would make
replay a lie, because evaluating the archive at a past moment would read a
confirmation from that moment's future.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "poll_cycle",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column(
            "started_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed", JSON_TYPE, nullable=False),
        sa.Column("failed", JSON_TYPE, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_poll_cycle"),
    )
    op.create_index(
        "ix_poll_cycle_channel_completed",
        "poll_cycle",
        ["venue", "channel", "completed_ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_poll_cycle_channel_completed", table_name="poll_cycle")
    op.drop_table("poll_cycle")
