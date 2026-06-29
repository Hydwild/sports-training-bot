"""
ORM-модели (SQLAlchemy 2.0, типизированный декларативный стиль).

Мультитенантность: каждая прикладная таблица содержит tenant_id и внешний
ключ на tenants. Все уникальные ограничения включают tenant_id, поэтому
данные разных клубов полностью изолированы (один и тот же Telegram-юзер
может состоять в разных клубах независимо).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Tenant(Base):
    """Клуб/организация — изолированный «мир»."""
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Привязка к источнику: например, telegram-чат или vk-группа.
    # Помогает определить тенанта по входящему апдейту.
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    vk_group_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    # Админ клуба
    admin_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_vk_id: Mapped[int | None] = mapped_column(BigInteger)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- White-label (брендинг клуба для админки) ---
    brand_name: Mapped[str | None] = mapped_column(String(200))
    brand_color: Mapped[str] = mapped_column(String(9), default="#3a7bd5")
    brand_logo_url: Mapped[str | None] = mapped_column(String(500))
    # Платёжный провайдер по умолчанию для этого клуба: yookassa | stripe
    payment_provider: Mapped[str] = mapped_column(String(20), default="yookassa")
    # --- Настройки уведомлений и поведения ---
    # Напоминание о тренировке: вкл и за сколько минут до начала
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reminder_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # Напоминание тренеру о неподтверждённых гостях (минут до начала; 0 — выкл)
    guest_reminder_minutes: Mapped[int] = mapped_column(Integer, default=120)
    # Авто-истечение неподтверждённых гостей: вкл и за сколько минут до начала
    guest_expire_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    guest_expire_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # Уведомлять подписчиков об открытии записи (публикация черновика)
    publish_notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Окно отмены: за сколько минут до тренировки запретить отписку (0 — без ограничений)
    cancel_lock_minutes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    trainings: Mapped[list[Training]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class Training(Base):
    __tablename__ = "trainings"
    __table_args__ = ()

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Pro: опциональная привязка к группе внутри клуба (дети/взрослые и т.п.)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    location: Mapped[str] = mapped_column(String(300), default="")
    max_participants: Mapped[int] = mapped_column(Integer)
    duration_min: Mapped[int] = mapped_column(Integer, default=120)
    # Цена участия в минимальных единицах валюты (копейки/центы). 0 — бесплатно.
    price_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    # published | draft
    state: Mapped[str] = mapped_column(String(20), default="published")
    publish_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # служебные флаги фоновых задач по гостям
    guest_reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    guests_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_platform: Mapped[str] = mapped_column(String(8))
    created_by_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    tenant: Mapped[Tenant] = relationship(back_populates="trainings")
    signups: Mapped[list[Signup]] = relationship(
        back_populates="training", cascade="all, delete-orphan"
    )


class Signup(Base):
    __tablename__ = "signups"
    __table_args__ = (
        # один участник на тренировку — уникален в пределах тенанта
        UniqueConstraint("tenant_id", "training_id", "platform", "user_id",
                         name="uq_signup_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    training_id: Mapped[int] = mapped_column(
        ForeignKey("trainings.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(8))   # tg | vk
    user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(200), default="")
    username: Mapped[str | None] = mapped_column(String(100))   # @никнейм
    photo_url: Mapped[str | None] = mapped_column(String(500))  # URL аватара
    status: Mapped[str] = mapped_column(String(10))    # active | queue
    position: Mapped[int] = mapped_column(Integer)
    attended: Mapped[bool] = mapped_column(Boolean, default=False)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    # --- Запись за гостя (человека без доступа к сети) ---
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    # для гостя: подтверждена ли тренером как реально занятая
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    # кто записал (для гостя — id записавшего участника)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    training: Mapped[Training] = relationship(back_populates="signups")


class Subscriber(Base):
    """Подписчик на рассылки в рамках тенанта."""
    __tablename__ = "subscribers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "platform", "user_id",
                         name="uq_subscriber_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(200), default="")
    username: Mapped[str | None] = mapped_column(String(100))   # @никнейм без @
    photo_url: Mapped[str | None] = mapped_column(String(500))  # URL аватара
    subscribed: Mapped[bool] = mapped_column(Boolean, default=True)


class Outbox(Base):
    """Очередь исходящих уведомлений (кросс-платформенная доставка)."""
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class Membership(Base):
    """
    Роль пользователя в клубе. Один Telegram-аккаунт может иметь роли в
    разных клубах. Роли: owner | coach | assistant.
    """
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tg_user_id", name="uq_membership_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20))   # owner | coach | assistant
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class Payment(Base):
    """Платёж за участие в тренировке."""
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    training_id: Mapped[int] = mapped_column(
        ForeignKey("trainings.id", ondelete="CASCADE"), index=True
    )
    signup_id: Mapped[int | None] = mapped_column(
        ForeignKey("signups.id", ondelete="SET NULL")
    )
    platform: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[int] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(String(20))   # yookassa | stripe
    # ID платежа на стороне провайдера (для идемпотентности вебхука)
    provider_payment_id: Mapped[str | None] = mapped_column(String(120), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    # pending | succeeded | canceled
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class Group(Base):
    """Группа внутри клуба (Pro): например «Дети», «Взрослые», «Продвинутые»."""
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_group_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
