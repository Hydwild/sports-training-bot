"""tenant last_digest_date

Revision ID: 9716603d6475
Revises: 7b9304577ac1
Create Date: 2026-07-20 21:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9716603d6475'
down_revision: Union[str, None] = '7b9304577ac1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants',
        sa.Column('last_digest_date', sa.String(length=10), nullable=False,
                  server_default=''))


def downgrade() -> None:
    op.drop_column('tenants', 'last_digest_date')
