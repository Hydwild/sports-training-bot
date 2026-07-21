"""catch-up: объекты, которые были только в create_all

Часть таблиц и колонок появлялась в моделях без миграции — схема в проде
создавалась Base.metadata.create_all при старте. Из-за этого `alembic
upgrade head` на ЧИСТОЙ базе давал неполную схему: приложение поднималось
только потому, что create_all дочищал остаток.

Здесь догоняем историю. Каждый шаг проверяет наличие объекта в базе:
на проде эти таблицы/колонки уже созданы create_all, и обычный add_column
упал бы с DuplicateColumn — ровно тот сценарий, который в прошлый раз
положил Railway в цикл перезапусков.

Revision ID: c41d0f2a7b6e
Revises: 569cea39a5c6
Create Date: 2026-07-21 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c41d0f2a7b6e'
down_revision: Union[str, None] = '569cea39a5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (таблица, колонка, тип, nullable, server_default)
_COLUMNS = [
    ('outbox', 'attempts', sa.Integer(), False, '0'),
    ('subscribers', 'alias', sa.String(length=200), True, None),
    ('tenants', 'welcome_text', sa.String(length=1000), True, None),
    ('tenants', 'signup_close_minutes', sa.Integer(), False, '0'),
    ('tenants', 'paid_until', sa.String(length=10), False, ''),
    ('tenants', 'last_billing_notice', sa.String(length=32), False, ''),
    ('tenants', 'tg_token', sa.String(length=200), True, None),
    ('tenants', 'vk_token', sa.String(length=200), True, None),
    ('trainings', 'group_message_id', sa.BigInteger(), True, None),
]


def _existing(inspector, table: str) -> set[str]:
    return {c['name'] for c in inspector.get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)

    if 'schedules' not in insp.get_table_names():
        op.create_table(
            'schedules',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('tenant_id', sa.Integer(), nullable=False),
            sa.Column('weekday', sa.Integer(), nullable=False),
            sa.Column('time_str', sa.String(length=5), nullable=False),
            sa.Column('title', sa.String(length=300), nullable=False),
            sa.Column('location', sa.String(length=300), nullable=False,
                      server_default=''),
            sa.Column('duration_min', sa.Integer(), nullable=False,
                      server_default='90'),
            sa.Column('price_minor', sa.Integer(), nullable=False,
                      server_default='0'),
            sa.Column('max_participants', sa.Integer(), nullable=False,
                      server_default='6'),
            sa.Column('days_ahead', sa.Integer(), nullable=False,
                      server_default='3'),
            sa.Column('active', sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column('last_date', sa.String(length=10), nullable=False,
                      server_default=''),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'],
                                    ondelete='CASCADE'),
        )
        op.create_index('ix_schedules_tenant_id', 'schedules', ['tenant_id'])

    tables = set(sa.inspect(conn).get_table_names())
    for table, column, type_, nullable, default in _COLUMNS:
        if table not in tables:
            continue
        if column in _existing(sa.inspect(conn), table):
            continue
        op.add_column(table, sa.Column(column, type_, nullable=nullable,
                                       server_default=default))


def downgrade() -> None:
    conn = op.get_bind()
    for table, column, *_ in reversed(_COLUMNS):
        insp = sa.inspect(conn)
        if table in insp.get_table_names() and column in _existing(insp, table):
            op.drop_column(table, column)
    if 'schedules' in sa.inspect(conn).get_table_names():
        op.drop_table('schedules')
