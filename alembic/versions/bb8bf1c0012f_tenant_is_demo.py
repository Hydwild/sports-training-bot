"""tenant is_demo

Revision ID: bb8bf1c0012f
Revises: 7d78baacf16d
Create Date: 2026-07-21 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'bb8bf1c0012f'
down_revision: Union[str, None] = '7d78baacf16d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants',
        sa.Column('is_demo', sa.Boolean(), nullable=False,
                  server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('tenants', 'is_demo')
