"""outbox: состояния доставки вместо одного флага sent

Одного булева sent не хватало: захваченное сообщение помечалось sent=True
ДО отправки, и перезапуск процесса посреди доставки терял его молча.
Недоставленные после лимита попыток тоже помечались sent=True — то есть
были неотличимы от успешных.

Revision ID: d3b7e91c2af4
Revises: c41d0f2a7b6e
Create Date: 2026-07-21 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd3b7e91c2af4'
down_revision: Union[str, None] = 'c41d0f2a7b6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = [
    ('status', sa.String(length=12), False, 'pending'),
    ('claimed_at', sa.DateTime(timezone=True), True, None),
    ('last_error', sa.String(length=300), False, ''),
]


def _columns(conn) -> set[str]:
    return {c['name'] for c in sa.inspect(conn).get_columns('outbox')}


def upgrade() -> None:
    # как и в догоняющей миграции: в проде схему создаёт create_all при
    # старте, поэтому колонки могут уже существовать
    conn = op.get_bind()
    have = _columns(conn)
    for name, type_, nullable, default in _COLUMNS:
        if name not in have:
            op.add_column('outbox', sa.Column(name, type_, nullable=nullable,
                                              server_default=default))
    indexes = {i['name'] for i in sa.inspect(conn).get_indexes('outbox')}
    if 'ix_outbox_status' not in indexes:
        op.create_index('ix_outbox_status', 'outbox', ['status'])
    # уже отправленные помечаем соответствующим состоянием
    op.execute("UPDATE outbox SET status = 'sent' WHERE sent")


def downgrade() -> None:
    conn = op.get_bind()
    if 'ix_outbox_status' in {i['name']
                              for i in sa.inspect(conn).get_indexes('outbox')}:
        op.drop_index('ix_outbox_status', table_name='outbox')
    have = _columns(conn)
    for name, *_ in reversed(_COLUMNS):
        if name in have:
            op.drop_column('outbox', name)
