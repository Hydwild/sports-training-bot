"""master reviews (рейтинг мастеров)

Revision ID: e461514ab40b
Revises: 888f744f773d
Create Date: 2026-07-21 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e461514ab40b'
down_revision: Union[str, None] = '888f744f773d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('master_reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('master_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('author_name', sa.String(length=120), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('text', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['master_id'], ['masters.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'master_id', 'user_id',
                        name='uq_master_review_author')
    )
    op.create_index(op.f('ix_master_reviews_tenant_id'), 'master_reviews',
                    ['tenant_id'], unique=False)
    op.create_index(op.f('ix_master_reviews_master_id'), 'master_reviews',
                    ['master_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_master_reviews_master_id'),
                  table_name='master_reviews')
    op.drop_index(op.f('ix_master_reviews_tenant_id'),
                  table_name='master_reviews')
    op.drop_table('master_reviews')
