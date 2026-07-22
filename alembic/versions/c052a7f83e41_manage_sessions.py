"""одноразовая manage-ссылка + короткая сессия (manage_sessions, used_at)

Раньше cookie хранила сам manage-токен (тот, что в адресе ссылки, живёт
до 90 дней), и вход по ссылке был многоразовым. Теперь ссылка одноразовая
(used_at), а в cookie кладётся отдельный секрет короткой сессии.

Revision ID: c052a7f83e41
Revises: b6e18d3c4a90
Create Date: 2026-07-22 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c052a7f83e41'
down_revision: Union[str, None] = 'b6e18d3c4a90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    have = {c['name'] for c in insp.get_columns('manage_tokens')}
    if 'used_at' not in have:
        op.add_column('manage_tokens',
                      sa.Column('used_at', sa.DateTime(timezone=True),
                                nullable=True))

    if 'manage_sessions' not in insp.get_table_names():
        op.create_table(
            'manage_sessions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('platform', sa.String(length=8), nullable=False,
                      server_default='web'),
            sa.Column('user_id', sa.BigInteger(), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'],
                                    ondelete='CASCADE'),
        )
        op.create_index('ix_manage_sessions_tenant_id', 'manage_sessions',
                        ['tenant_id'])
        op.create_index('ix_manage_sessions_user_id', 'manage_sessions',
                        ['user_id'])
        op.create_index('ix_manage_sessions_token_hash', 'manage_sessions',
                        ['token_hash'], unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'manage_sessions' in insp.get_table_names():
        op.drop_table('manage_sessions')
    have = {c['name'] for c in insp.get_columns('manage_tokens')}
    if 'used_at' in have:
        op.drop_column('manage_tokens', 'used_at')
