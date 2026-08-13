"""M2 evaluations: persist every pricing decision

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-13

Rejections are stored alongside acceptances, and that is the point. "Nothing
qualified today" is a useless sentence; a countable set of reason codes is a
finding. The falsification report is built from this table, and a table of
acceptances alone would be a list of successes with no denominator -- which is
how a strategy convinces itself it works.

Each row carries the fee rule, parser version, and staleness threshold it was
decided under, so a decision stays re-derivable after any of them changes.

``relationship_id`` is RESTRICT: a relationship with decisions behind it cannot
be deleted out from under them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
MONEY = sa.Numeric(precision=18, scale=8)


def upgrade() -> None:
    op.create_table(
        "evaluation",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "evaluated_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("relationship_id", sa.Uuid(), nullable=True),
        sa.Column("relationship_slug", sa.String(length=128), nullable=False),
        sa.Column("relationship_version", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("acquisition_cost", MONEY, nullable=False),
        sa.Column("fees", MONEY, nullable=False),
        sa.Column("reserves", MONEY, nullable=False),
        sa.Column("guaranteed_payout", MONEY, nullable=False),
        sa.Column("net_edge", MONEY, nullable=False),
        sa.Column("legs", JSON_TYPE, nullable=False),
        sa.Column("fee_rule", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.Column("max_book_age_ms", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["relationship_id"],
            ["relationship.id"],
            name=op.f("fk_evaluation_relationship_id_relationship"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation")),
    )
    op.create_index("ix_evaluation_accepted", "evaluation", ["accepted", "evaluated_ts"])
    op.create_index("ix_evaluation_reason", "evaluation", ["reason", "evaluated_ts"])
    op.create_index("ix_evaluation_slug_ts", "evaluation", ["relationship_slug", "evaluated_ts"])


def downgrade() -> None:
    op.drop_index("ix_evaluation_slug_ts", table_name="evaluation")
    op.drop_index("ix_evaluation_reason", table_name="evaluation")
    op.drop_index("ix_evaluation_accepted", table_name="evaluation")
    op.drop_table("evaluation")
