"""tenant public profile (cover, about, address, phone)

Revision ID: 888f744f773d
Revises: 9716603d6475
Create Date: 2026-07-21 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '888f744f773d'
down_revision: Union[str, None] = '9716603d6475'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('cover_url', sa.String(length=500),
                                       nullable=True))
    op.add_column('tenants', sa.Column('about', sa.String(length=2000),
                                       nullable=True))
    op.add_column('tenants', sa.Column('address', sa.String(length=300),
                                       nullable=True))
    op.add_column('tenants', sa.Column('contact_phone', sa.String(length=32),
                                       nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'contact_phone')
    op.drop_column('tenants', 'address')
    op.drop_column('tenants', 'about')
    op.drop_column('tenants', 'cover_url')
