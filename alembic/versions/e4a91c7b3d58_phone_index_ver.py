"""версия ключа индекса телефонов (web_customers.index_ver)

Индекс поиска считался текущим ключом, поэтому добавление PHONE_ENC_KEY
«теряло» строки, проиндексированные выведенным из JWT ключом, и на том же
номере заводился дубль клиента. Версия индекса хранится отдельно от
версии шифротекста: во время перехода строка может быть уже перешифрована,
но ещё не переиндексирована.

Существующие строки индексированы тем же ключом, что и шифротекст, —
поэтому index_ver заполняется значением key_ver.

Revision ID: e4a91c7b3d58
Revises: b58e2c7a91f4
Create Date: 2026-07-22 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4a91c7b3d58'
down_revision: Union[str, None] = 'b58e2c7a91f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if 'web_customers' not in sa.inspect(conn).get_table_names():
        return
    have = {c['name'] for c in sa.inspect(conn).get_columns('web_customers')}
    if 'index_ver' in have:
        return
    op.add_column('web_customers',
                  sa.Column('index_ver', sa.String(length=8), nullable=False,
                            server_default='jwt'))
    # индекс считался тем же ключом, что и шифротекст
    op.execute("UPDATE web_customers SET index_ver = key_ver")


def downgrade() -> None:
    conn = op.get_bind()
    if 'web_customers' not in sa.inspect(conn).get_table_names():
        return
    have = {c['name'] for c in sa.inspect(conn).get_columns('web_customers')}
    if 'index_ver' in have:
        op.drop_column('web_customers', 'index_ver')
