"""журнал согласий (consent_events)

Галочку на форме проверяли, но факт согласия нигде не сохраняли —
доказать, что человек её ставил, было нечем.

Revision ID: d81b4e6f2c95
Revises: c73f1a5e9b24
Create Date: 2026-07-22 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd81b4e6f2c95'
down_revision: Union[str, None] = 'c73f1a5e9b24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # таблицу мог уже создать create_all при старте приложения
    if 'consent_events' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'consent_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        # null — общеплатформенная форма (отзыв о сервисе)
        sa.Column('tenant_id', sa.Integer(), nullable=True),
        sa.Column('platform', sa.String(length=8), nullable=False,
                  server_default='web'),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('purpose', sa.String(length=32), nullable=False),
        sa.Column('policy_version', sa.String(length=20), nullable=False),
        sa.Column('consent_text', sa.String(length=500), nullable=False,
                  server_default=''),
        sa.Column('source', sa.String(length=32), nullable=False,
                  server_default='web-form'),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'],
                                ondelete='CASCADE'),
    )
    op.create_index('ix_consent_events_tenant_id', 'consent_events',
                    ['tenant_id'])
    op.create_index('ix_consent_events_user_id', 'consent_events', ['user_id'])
    op.create_index('ix_consent_events_purpose', 'consent_events', ['purpose'])


def downgrade() -> None:
    if 'consent_events' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table('consent_events')
