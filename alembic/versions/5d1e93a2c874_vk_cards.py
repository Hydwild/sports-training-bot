"""vk_cards: адреса отправленных в VK карточек, чтобы обновлять их позже

Revision ID: 5d1e93a2c874
Revises: 4c8d21f7b063
Create Date: 2026-07-23 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5d1e93a2c874"
down_revision: Union[str, None] = "4c8d21f7b063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "vk_cards" in inspector.get_table_names():
        return
    op.create_table(
        "vk_cards",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer,
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("training_id", sa.Integer,
                  sa.ForeignKey("trainings.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("peer_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        # значение проставляет модель (default=_utcnow), поэтому NOT NULL —
        # иначе схема разъезжается с моделью и это ловит `alembic check`
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # одна карточка на человека и занятие: повторная отправка заменяет
        # адрес, а не плодит строки
        sa.UniqueConstraint("tenant_id", "training_id", "peer_id",
                            name="uq_vk_card_target"),
    )
    op.create_index("ix_vk_cards_tenant_id", "vk_cards", ["tenant_id"])
    op.create_index("ix_vk_cards_training_id", "vk_cards", ["training_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "vk_cards" not in inspector.get_table_names():
        return
    op.drop_index("ix_vk_cards_training_id", table_name="vk_cards")
    op.drop_index("ix_vk_cards_tenant_id", table_name="vk_cards")
    op.drop_table("vk_cards")
