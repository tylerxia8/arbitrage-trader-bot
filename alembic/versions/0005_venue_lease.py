"""Share one venue request budget across every process that spends it

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-14

The venue rate-limits per IP. This system rate-limited per component, and those
are not the same denominator. On 2026-08-14 a collector at roughly four
requests a second, a one-second probe at six, a proposal sweep and a venue-wide
survey at five ran at once. Each was comfortably inside the ceiling its own
limiter knew about. Together they were well outside the one the venue enforces,
the production host began resetting TLS handshakes, and fifteen and a half
hours of collection were lost along with the seven-day exit gate.

A rate limit enforced per process is not enforced. The database is the only
thing every consumer shares -- the collector runs in a container, the probe and
survey on the host -- so the arithmetic has to live here.

Leases heartbeat rather than being held to completion. A consumer that dies
without releasing would otherwise lock the venue out until a human noticed,
which is a worse outage than the one this prevents.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "venue_lease",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("consumer", sa.String(length=64), nullable=False),
        sa.Column("requests_per_second", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column(
            "started_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_venue_lease"),
    )
    op.create_index("ix_venue_lease_venue_heartbeat", "venue_lease", ["venue", "heartbeat_ts"])


def downgrade() -> None:
    op.drop_index("ix_venue_lease_venue_heartbeat", table_name="venue_lease")
    op.drop_table("venue_lease")
