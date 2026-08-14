"""Persist order intents and leg orders

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-14

The risk gate sizes every candidate against what is currently open, and until
now "what is currently open" was whatever the caller passed in. A limit
enforced against a number the caller supplies is not a limit. These tables are
where that number comes from.

Two constraints carry most of the weight.

``order_intent.intent_id`` is unique, because leg idempotency keys are derived
from it and reusing one deliberately reuses the venue's view of those orders.

``leg_order.idempotency_key`` is unique in the database rather than merely
generated carefully. A retried submit that fills twice turns one leg of a
hedged basket into a directional position, which is the most expensive failure
available to this system -- too expensive to defend with discipline alone. The
constraint makes a second insert under the same key fail loudly instead of
succeeding quietly.

Rows are written before the first leg is sent, not after the last one returns.
A process that dies mid-acquisition has left real positions at the venue, and a
record written afterwards would be missing for exactly the intents that most
need one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(precision=18, scale=8)


def upgrade() -> None:
    op.create_table(
        "order_intent",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("relationship_slug", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("notional", MONEY, nullable=False),
        sa.Column("net_edge", MONEY, nullable=False),
        sa.Column("spent", MONEY, nullable=False, server_default="0"),
        sa.Column("recovered", MONEY, nullable=False, server_default="0"),
        sa.Column(
            "created_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_intent"),
        sa.UniqueConstraint("intent_id", name="uq_order_intent_intent_id"),
    )
    op.create_index("ix_order_intent_state", "order_intent", ["state", "updated_ts"])

    op.create_table(
        "leg_order",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("intent_row_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=128), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False, server_default="buy"),
        sa.Column("limit_price", MONEY, nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("filled", MONEY, nullable=False, server_default="0"),
        sa.Column("cost", MONEY, nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("venue_order_id", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "submitted_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["intent_row_id"],
            ["order_intent.id"],
            name="fk_leg_order_intent_row_id_order_intent",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_leg_order"),
        sa.UniqueConstraint("idempotency_key", name="uq_leg_order_idempotency_key"),
    )
    op.create_index("ix_leg_order_intent_row_id", "leg_order", ["intent_row_id"])
    op.create_index("ix_leg_order_ticker", "leg_order", ["ticker", "submitted_ts"])


def downgrade() -> None:
    op.drop_index("ix_leg_order_ticker", table_name="leg_order")
    op.drop_index("ix_leg_order_intent_row_id", table_name="leg_order")
    op.drop_table("leg_order")
    op.drop_index("ix_order_intent_state", table_name="order_intent")
    op.drop_table("order_intent")
