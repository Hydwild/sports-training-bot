"""master bio (описание под фото)

Revision ID: 569cea39a5c6
Revises: e461514ab40b
Create Date: 2026-07-21 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '569cea39a5c6'
down_revision: Union[str, None] = 'e461514ab40b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('masters',
        sa.Column('bio', sa.String(length=500), nullable=False,
                  server_default=''))


def downgrade() -> None:
    op.drop_column('masters', 'bio')
