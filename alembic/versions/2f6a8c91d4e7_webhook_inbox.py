"""per-tenant webhook modes and durable inbound inbox

Revision ID: 2f6a8c91d4e7
Revises: c052a7f83e41
Create Date: 2026-07-22 21:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2f6a8c91d4e7"
down_revision: Union[str, None] = "c052a7f83e41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tenant_columns = {c["name"] for c in inspector.get_columns("tenants")}
    if "tg_delivery_mode" not in tenant_columns:
        op.add_column("tenants", sa.Column(
            "tg_delivery_mode", sa.String(12), nullable=False,
            server_default="polling",
        ))
    if "vk_delivery_mode" not in tenant_columns:
        op.add_column("tenants", sa.Column(
            "vk_delivery_mode", sa.String(12), nullable=False,
            server_default="longpoll",
        ))
    if "vk_confirmation_code" not in tenant_columns:
        op.add_column("tenants", sa.Column(
            "vk_confirmation_code", sa.String(64), nullable=False,
            server_default="",
        ))

    if "inbound_events" not in inspector.get_table_names():
        op.create_table(
            "inbound_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("platform", sa.String(8), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("external_event_id", sa.String(160), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("status", sa.String(12), nullable=False,
                      server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("processed_at", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.String(500), nullable=False,
                      server_default=""),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                    ondelete="CASCADE"),
            sa.UniqueConstraint("platform", "tenant_id", "external_event_id",
                                name="uq_inbound_event_external"),
        )
        op.create_index("ix_inbound_events_platform", "inbound_events", ["platform"])
        op.create_index("ix_inbound_events_tenant_id", "inbound_events", ["tenant_id"])
        op.create_index("ix_inbound_events_status", "inbound_events", ["status"])
        op.create_index("ix_inbound_events_next_attempt_at", "inbound_events",
                        ["next_attempt_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "inbound_events" in inspector.get_table_names():
        op.drop_table("inbound_events")
    tenant_columns = {c["name"] for c in inspector.get_columns("tenants")}
    for name in ("vk_confirmation_code", "vk_delivery_mode", "tg_delivery_mode"):
        if name in tenant_columns:
            op.drop_column("tenants", name)
