"""outbox.handled_at — когда сообщение пришло в конечное состояние

Нужна для retention: доставленные и отброшенные оператором сообщения
чистятся по этой отметке, а не по дате создания.

Revision ID: a94c3f7e2b18
Revises: f26d9a4c1e73
Create Date: 2026-07-22 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a94c3f7e2b18'
down_revision: Union[str, None] = 'f26d9a4c1e73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    have = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('outbox')}
    if 'handled_at' not in have:
        op.add_column('outbox', sa.Column('handled_at',
                                          sa.DateTime(timezone=True),
                                          nullable=True))


def downgrade() -> None:
    have = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('outbox')}
    if 'handled_at' in have:
        op.drop_column('outbox', 'handled_at')
