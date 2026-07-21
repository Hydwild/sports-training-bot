"""зашифрованные токены ботов (tenants.*_token_enc)

Токен бота — полный контроль над ботом клуба: чтение переписки, рассылка
от его имени, смена вебхука. Он лежал открытым текстом и попадал в каждый
дамп базы, а дамп уходит в Telegram.

Здесь только НОВЫЕ колонки. Открытые остаются на месте: код читает оба
формата, перенос делает scripts/migrate_bot_tokens.py, а удаление старых
колонок — отдельная миграция после подтверждения (см. DISASTER_RECOVERY.md).

Revision ID: c73f1a5e9b24
Revises: e4a91c7b3d58
Create Date: 2026-07-22 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c73f1a5e9b24'
down_revision: Union[str, None] = 'e4a91c7b3d58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Text, а не String(200): шифротекст Fernet длиннее исходного токена
_COLUMNS = [
    ('tg_token_enc', sa.Text(), ''),
    ('tg_token_ver', sa.String(length=8), ''),
    ('vk_token_enc', sa.Text(), ''),
    ('vk_token_ver', sa.String(length=8), ''),
]


def upgrade() -> None:
    conn = op.get_bind()
    have = {c['name'] for c in sa.inspect(conn).get_columns('tenants')}
    for name, type_, default in _COLUMNS:
        if name not in have:
            op.add_column('tenants', sa.Column(name, type_, nullable=False,
                                               server_default=default))


def downgrade() -> None:
    conn = op.get_bind()
    have = {c['name'] for c in sa.inspect(conn).get_columns('tenants')}
    for name, *_ in reversed(_COLUMNS):
        if name in have:
            op.drop_column('tenants', name)
