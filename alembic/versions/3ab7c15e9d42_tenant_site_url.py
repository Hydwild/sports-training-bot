"""tenant.site_url: свой адрес страницы записи у клиента

Revision ID: 3ab7c15e9d42
Revises: 2f6a8c91d4e7
Create Date: 2026-07-23 06:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3ab7c15e9d42"
down_revision: Union[str, None] = "2f6a8c91d4e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("tenants")}
    if "site_url" not in columns:
        # nullable: пусто = наша страница /club/<id>, как было до сих пор
        op.add_column("tenants", sa.Column("site_url", sa.String(500),
                                           nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("tenants")}
    if "site_url" in columns:
        op.drop_column("tenants", "site_url")
