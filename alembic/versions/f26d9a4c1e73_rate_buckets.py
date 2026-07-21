"""общий счётчик частоты запросов (rate_buckets)

Счётчик жил в памяти процесса: два воркера давали двойной лимит, а
перезапуск обнулял счёт.

Revision ID: f26d9a4c1e73
Revises: d81b4e6f2c95
Create Date: 2026-07-22 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f26d9a4c1e73'
down_revision: Union[str, None] = 'd81b4e6f2c95'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'rate_buckets' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'rate_buckets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bucket_key', sa.String(length=200), nullable=False),
        sa.Column('window_start', sa.BigInteger(), nullable=False),
        sa.Column('hits', sa.Integer(), nullable=False, server_default='0'),
        # уникальность нужна не для порядка, а для работы механизма:
        # атомарный инкремент делается через ON CONFLICT по этой паре
        sa.UniqueConstraint('bucket_key', 'window_start',
                            name='uq_rate_bucket'),
    )
    op.create_index('ix_rate_buckets_bucket_key', 'rate_buckets',
                    ['bucket_key'])
    op.create_index('ix_rate_buckets_window_start', 'rate_buckets',
                    ['window_start'])


def downgrade() -> None:
    if 'rate_buckets' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table('rate_buckets')
