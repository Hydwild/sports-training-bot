"""reviews

Revision ID: 7d78baacf16d
Revises: e1b635257996
Create Date: 2026-07-19 15:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7d78baacf16d'
down_revision: Union[str, None] = 'e1b635257996'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('club_name', sa.String(length=160), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('approved', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reviews_approved'), 'reviews', ['approved'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_reviews_approved'), table_name='reviews')
    op.drop_table('reviews')
