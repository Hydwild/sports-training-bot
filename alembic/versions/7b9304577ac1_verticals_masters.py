"""verticals and masters

Revision ID: 7b9304577ac1
Revises: bb8bf1c0012f
Create Date: 2026-07-20 20:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7b9304577ac1'
down_revision: Union[str, None] = 'bb8bf1c0012f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants',
        sa.Column('vertical', sa.String(length=20), nullable=False,
                  server_default='sport'))
    op.create_table('masters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('specialty', sa.String(length=160), nullable=False),
    sa.Column('photo_url', sa.String(length=500), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_masters_tenant_id'), 'masters', ['tenant_id'],
                    unique=False)
    # SQLite не умеет ADD COLUMN с FK — batch_alter_table пересоздаёт таблицу;
    # на Postgres выполняется как обычный ALTER TABLE
    with op.batch_alter_table('trainings') as batch:
        batch.add_column(sa.Column('master_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_trainings_master_id', 'masters',
                                 ['master_id'], ['id'], ondelete='SET NULL')
    op.create_index(op.f('ix_trainings_master_id'), 'trainings', ['master_id'],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_trainings_master_id'), table_name='trainings')
    with op.batch_alter_table('trainings') as batch:
        batch.drop_constraint('fk_trainings_master_id', type_='foreignkey')
        batch.drop_column('master_id')
    op.drop_index(op.f('ix_masters_tenant_id'), table_name='masters')
    op.drop_table('masters')
    op.drop_column('tenants', 'vertical')
