"""outbox: пауза между повторами (next_attempt_at)

Неудачная доставка повторялась на каждом проходе очереди — заблокированный
бот перебирался пять раз подряд за минуту и мгновенно попадал в dead.

Revision ID: b58e2c7a91f4
Revises: a17f6b9c4d20
Create Date: 2026-07-21 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b58e2c7a91f4'
down_revision: Union[str, None] = 'a17f6b9c4d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    have = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('outbox')}
    if 'next_attempt_at' not in have:
        op.add_column('outbox', sa.Column('next_attempt_at',
                                          sa.DateTime(timezone=True),
                                          nullable=True))


def downgrade() -> None:
    have = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('outbox')}
    if 'next_attempt_at' in have:
        op.drop_column('outbox', 'next_attempt_at')
