"""персональные ссылки управления записями (manage_tokens)

Revision ID: f92c4a1d5e73
Revises: d3b7e91c2af4
Create Date: 2026-07-21 20:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f92c4a1d5e73'
down_revision: Union[str, None] = 'd3b7e91c2af4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # таблицу мог уже создать create_all при старте приложения (см. main.py)
    if 'manage_tokens' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'manage_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=8), nullable=False,
                  server_default='web'),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'],
                                ondelete='CASCADE'),
    )
    op.create_index('ix_manage_tokens_tenant_id', 'manage_tokens', ['tenant_id'])
    op.create_index('ix_manage_tokens_user_id', 'manage_tokens', ['user_id'])
    op.create_index('ix_manage_tokens_token_hash', 'manage_tokens',
                    ['token_hash'], unique=True)


def downgrade() -> None:
    if 'manage_tokens' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table('manage_tokens')
