"""Separate "the economics cleared" from "this may be traded"

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-14

Live detection writes a row for every basket it prices, and one boolean could
not carry both questions honestly.

Recording only tradeability makes every row read ``relationship_not_approved``
until a reviewer signs, so an archive gathered during review learns nothing
about whether an edge existed. Recording only economics suggests a tradeable
opportunity nobody has signed for, which is the exact claim FR-005 exists to
prevent.

So ``accepted`` means the economics cleared -- depth, freshness, fees, net
edge -- and ``tradeable`` means that *and* an approved relationship covered the
leg set. ``tradeable`` is the only column an execution path may read.

``relationship_status`` is stamped at decision time so that approving a
relationship later cannot make a past rejection look as though it had been
tradeable all along.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation",
        sa.Column("tradeable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "evaluation",
        sa.Column(
            "relationship_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_index("ix_evaluation_tradeable", "evaluation", ["tradeable", "evaluated_ts"])


def downgrade() -> None:
    op.drop_index("ix_evaluation_tradeable", table_name="evaluation")
    op.drop_column("evaluation", "relationship_status")
    op.drop_column("evaluation", "tradeable")
