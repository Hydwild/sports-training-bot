"""служебное хранилище ключ-значение (platform_state)

Одиночные факты уровня площадки — например, дата последнего успешного
restore drill.

Revision ID: b6e18d3c4a90
Revises: a94c3f7e2b18
Create Date: 2026-07-22 20:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6e18d3c4a90'
down_revision: Union[str, None] = 'a94c3f7e2b18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if 'platform_state' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'platform_state',
        sa.Column('key', sa.String(length=64), primary_key=True),
        sa.Column('value', sa.String(length=300), nullable=False,
                  server_default=''),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    if 'platform_state' in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table('platform_state')
