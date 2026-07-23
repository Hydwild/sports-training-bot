"""tenant.slug и tenant.bot_username: читаемый адрес и ссылка на бота

Revision ID: 4c8d21f7b063
Revises: 3ab7c15e9d42
Create Date: 2026-07-23 07:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4c8d21f7b063"
down_revision: Union[str, None] = "3ab7c15e9d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("tenants")}
    indexes = {i["name"] for i in inspector.get_indexes("tenants")}

    if "slug" not in columns:
        op.add_column("tenants", sa.Column("slug", sa.String(40),
                                           nullable=True))
    # Уникальность на уровне БД, а не только формы: два клуба с одним
    # адресом означали бы, что посетитель попадает не туда. NULL при этом
    # не конфликтуют — адрес необязателен.
    if "ix_tenants_slug" not in indexes:
        op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    if "bot_username" not in columns:
        op.add_column("tenants", sa.Column("bot_username", sa.String(64),
                                           nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("tenants")}
    indexes = {i["name"] for i in inspector.get_indexes("tenants")}

    if "ix_tenants_slug" in indexes:
        op.drop_index("ix_tenants_slug", table_name="tenants")
    for name in ("bot_username", "slug"):
        if name in columns:
            op.drop_column("tenants", name)
