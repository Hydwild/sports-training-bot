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
    welcome_text: Mapped[str | None] = mapped_column(String(1000))
    signup_close_minutes: Mapped[int] = mapped_column(Integer, default=0)
    paid_until: Mapped[str] = mapped_column(String(10), default="")  # SaaS: ISO-дата
    # SaaS: маркер последнего отправленного клиенту уведомления об оплате,
    # вида "2026-08-01:soon" / "2026-08-01:expired" — привязан к текущему
    # paid_until, поэтому автоматически «сбрасывается» при продлении оплаты
    # (новое значение paid_until не совпадёт со старым маркером).
    last_billing_notice: Mapped[str] = mapped_column(String(32), default="")
    # мультиклиент: собственные боты клуба (пусто — используется бот из env)
    tg_token: Mapped[str | None] = mapped_column(String(200))
    vk_token: Mapped[str | None] = mapped_column(String(200))
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
    # Демо-клуб: любой написавший боту может выбрать роль "тренер" (получает
    # Membership с role=coach) или "участник" — см. app/bots/telegram.py
    # (_resolve_tenant, cmd_start) и app/services/tasks.py (ночной сброс).
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    # Вертикаль бизнеса: sport | beauty (терминология бота и страницы
    # записи — см. app/core/verticals.py)
    vertical: Mapped[str] = mapped_column(String(20), default="sport")
    # Дата (ISO, в таймзоне клуба) последнего утреннего дайджеста админу —
    # маркер «на сегодня уже отправляли» (см. tasks._admin_daily_digest)
    last_digest_date: Mapped[str] = mapped_column(String(10), default="")
    # --- Витрина на публичной странице записи (/club/{id}) ---
    cover_url: Mapped[str | None] = mapped_column(String(500))   # фото-обложка
    about: Mapped[str | None] = mapped_column(String(2000))      # описание
    address: Mapped[str | None] = mapped_column(String(300))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
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
    # Опциональный мастер/тренер, ведущий это время (салоны: барбер,
    # мастер маникюра и т.п.) — показывается на странице записи
    master_id: Mapped[int | None] = mapped_column(
        ForeignKey("masters.id", ondelete="SET NULL"), index=True
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
    # id сообщения-карточки, опубликованного в группе Telegram (для live-обновления)
    group_message_id: Mapped[int | None] = mapped_column(BigInteger)
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
    alias: Mapped[str | None] = mapped_column(String(200))      # подпись от тренера
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
    # состояние доставки, см. tasks.py:
    #   pending    — ждёт отправки
    #   processing — захвачено рабочим циклом, отправляется прямо сейчас
    #   sent       — доставлено
    #   dead       — не доставлено после MAX_OUTBOX_ATTEMPTS попыток
    # Отдельное «processing» нужно, чтобы перезапуск процесса посреди
    # отправки не терял сообщение молча: зависшие захваты возвращаются в
    # pending по времени (claimed_at), а не пропадают навсегда.
    status: Mapped[str] = mapped_column(String(12), default="pending",
                                        index=True)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True))
    # когда имеет смысл повторить: после неудачи пауза растёт (1, 2, 5, 15
    # минут), иначе заблокированный бот перебирается на каждом проходе
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(String(300), default="")
    # число неудачных попыток доставки; после лимита сообщение помечается
    # dead, чтобы не ретраить вечно (например, бот заблокирован юзером)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
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


class Schedule(Base):
    """Регулярное расписание: шаблон еженедельной тренировки.
    Фоновая задача создаёт по нему тренировки автоматически."""
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)          # 0=Пн … 6=Вс
    time_str: Mapped[str] = mapped_column(String(5))       # "19:00"
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str] = mapped_column(String(300), default="")
    duration_min: Mapped[int] = mapped_column(Integer, default=90)
    price_minor: Mapped[int] = mapped_column(Integer, default=0)
    max_participants: Mapped[int] = mapped_column(Integer, default=6)
    days_ahead: Mapped[int] = mapped_column(Integer, default=3)  # за сколько дней создавать
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_date: Mapped[str] = mapped_column(String(10), default="")  # ISO даты последнего создания
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class Master(Base):
    """Мастер/специалист клуба (салоны: барбер, мастер маникюра; спорт:
    тренер). Привязывается к слоту через Training.master_id — на странице
    записи показываются имя, специализация и фото."""
    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    specialty: Mapped[str] = mapped_column(String(160), default="")
    # описание под фото: опыт, регалии («парикмахер, опыт 3 года...»)
    bio: Mapped[str] = mapped_column(String(500), default="")
    photo_url: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class MasterReview(Base):
    """Оценка мастера клиентом с публичной страницы записи. Анти-накрутка:
    одна оценка на телефон (user_id — числовой id из телефона, как в
    веб-записи), повторная оценка ЗАМЕНЯЕТ прежнюю, а не добавляется."""
    __tablename__ = "master_reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "master_id", "user_id",
                         name="uq_master_review_author"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)   # телефон как id
    author_name: Mapped[str] = mapped_column(String(120))
    rating: Mapped[int] = mapped_column(Integer)       # 1..5
    text: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class WebCustomer(Base):
    """Клиент, записавшийся через сайт.

    Раньше идентификатором записи служил сам телефон (`user_id = int(цифры)`)
    — номер лежал в signups, subscribers и оценках открытым текстом и уезжал
    в каждую резервную копию. Теперь запись ссылается на суррогатный id, а
    номер хранится здесь: зашифрованным (phone_enc) и с детерминированным
    индексом для поиска (phone_index). См. app/core/phones.py."""
    __tablename__ = "web_customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_index", name="uq_web_customer"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    phone_index: Mapped[str] = mapped_column(String(64), index=True)
    phone_enc: Mapped[str] = mapped_column(Text, default="")
    key_ver: Mapped[str] = mapped_column(String(8), default="jwt")
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class ManageToken(Base):
    """Персональная ссылка управления своими записями (веб-запись).

    Сам токен случайный и нигде не хранится — в базе только его SHA-256.
    Утечка дампа не даёт доступа к чужим записям, а при удалении данных
    ссылку можно отозвать (revoked), чего нельзя сделать с выводимой из
    телефона HMAC-подписью."""
    __tablename__ = "manage_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(8), default="web")
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class Review(Base):
    """Отзыв о сервисе (не о конкретном клубе) — публикуется на /reviews
    только после ручного одобрения оператором площадки (защита от спама и
    накрутки)."""
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    club_name: Mapped[str] = mapped_column(String(160), default="")
    rating: Mapped[int] = mapped_column(Integer)  # 1..5
    text: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
