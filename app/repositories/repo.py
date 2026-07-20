"""
Слой репозиториев. Инкапсулирует доступ к данным.

Главный приём мультитенантности: TenantRepository привязан к конкретному
tenant_id, и КАЖДЫЙ запрос фильтруется по нему. Сервисный слой не может
случайно прочитать или изменить данные чужого клуба.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    Membership,
    Review,
    Schedule,
    Outbox,
    Payment,
    Signup,
    Subscriber,
    Tenant,
    Training,
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TenantRepository:
    """Все методы работают только в пределах одного тенанта."""

    def __init__(self, session: AsyncSession, tenant_id: int) -> None:
        self.session = session
        self.tenant_id = tenant_id

    # ---------- Тренировки ----------

    async def add_training(self, **kwargs) -> Training:
        training = Training(tenant_id=self.tenant_id, **kwargs)
        self.session.add(training)
        await self.session.flush()
        return training

    async def get_training(self, training_id: int) -> Training | None:
        stmt = select(Training).where(
            Training.id == training_id,
            Training.tenant_id == self.tenant_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_training_for_update(self, training_id: int) -> Training | None:
        """Как get_training, но блокирует строку (FOR UPDATE) — защита от
        одновременных записей на одну тренировку (гонки). В SQLite no-op."""
        stmt = select(Training).where(
            Training.id == training_id,
            Training.tenant_id == self.tenant_id,
        ).with_for_update()
        try:
            return (await self.session.execute(stmt)).scalar_one_or_none()
        except Exception:
            # SQLite и некоторые драйверы не поддерживают FOR UPDATE
            return await self.get_training(training_id)

    async def list_upcoming(self, include_drafts: bool = False) -> list[Training]:
        stmt = select(Training).where(
            Training.tenant_id == self.tenant_id,
            Training.is_cancelled.is_(False),
            Training.start_at >= _utcnow(),
        )
        if not include_drafts:
            stmt = stmt.where(Training.state == "published")
        stmt = stmt.order_by(Training.start_at.asc())
        return list((await self.session.execute(stmt)).scalars())

    async def list_past(self, limit: int = 10) -> list[Training]:
        """Прошедшие тренировки (для архива/истории), новые сверху."""
        stmt = select(Training).where(
            Training.tenant_id == self.tenant_id,
            Training.is_cancelled.is_(False),
            Training.start_at < _utcnow(),
            Training.state == "published",
        ).order_by(Training.start_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def list_drafts(self) -> list[Training]:
        stmt = select(Training).where(
            Training.tenant_id == self.tenant_id,
            Training.state == "draft",
            Training.is_cancelled.is_(False),
        ).order_by(Training.start_at.asc())
        return list((await self.session.execute(stmt)).scalars())

    # ---------- Записи ----------

    async def get_signups(self, training_id: int,
                          status: str | None = None) -> list[Signup]:
        stmt = select(Signup).where(
            Signup.tenant_id == self.tenant_id,
            Signup.training_id == training_id,
        )
        if status:
            stmt = stmt.where(Signup.status == status)
        stmt = stmt.order_by(Signup.position.asc())
        return list((await self.session.execute(stmt)).scalars())

    async def get_user_signup(self, training_id: int, platform: str,
                              user_id: int) -> Signup | None:
        stmt = select(Signup).where(
            Signup.tenant_id == self.tenant_id,
            Signup.training_id == training_id,
            Signup.platform == platform,
            Signup.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_signup_by_id(self, signup_id: int) -> Signup | None:
        stmt = select(Signup).where(
            Signup.id == signup_id,
            Signup.tenant_id == self.tenant_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_signup(self, **kwargs) -> Signup:
        s = Signup(tenant_id=self.tenant_id, **kwargs)
        self.session.add(s)
        await self.session.flush()
        return s

    async def delete_signup(self, signup: Signup) -> None:
        await self.session.delete(signup)
        await self.session.flush()

    async def count_active(self, training_id: int) -> int:
        stmt = select(func.count()).select_from(Signup).where(
            Signup.tenant_id == self.tenant_id,
            Signup.training_id == training_id,
            Signup.status == "active",
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # ---------- Подписчики ----------

    async def upsert_subscriber(self, platform: str, user_id: int,
                                name: str, username: str | None = None,
                                photo_url: str | None = None) -> None:
        stmt = select(Subscriber).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.platform == platform,
            Subscriber.user_id == user_id,
        )
        sub = (await self.session.execute(stmt)).scalar_one_or_none()
        if sub:
            sub.name = name
            if username is not None:
                sub.username = username
            if photo_url is not None:
                sub.photo_url = photo_url
            # если тренер задал подпись — не даём Telegram-имени её перетереть
            if getattr(sub, "alias", None):
                pass  # отображаемое имя в записях остаётся alias
        else:
            self.session.add(Subscriber(
                tenant_id=self.tenant_id, platform=platform,
                user_id=user_id, name=name, username=username,
                photo_url=photo_url, subscribed=True,
            ))
        await self.session.flush()

    async def aliases_map(self, platform: str = "tg") -> dict[int, str]:
        """{user_id: alias} для всех участников клуба с заданной подписью."""
        stmt = select(Subscriber.user_id, Subscriber.alias).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.platform == platform,
            Subscriber.alias.is_not(None),
        )
        rows = (await self.session.execute(stmt)).all()
        return {uid: alias for uid, alias in rows}

    async def aliases_map_all(self) -> dict[tuple[str, int], str]:
        """{(platform, user_id): alias} — подписи участников ВСЕХ платформ.
        Для карточки тренера: на одну тренировку могут быть записаны люди из
        tg, vk и web одновременно (у web-записей в подписи хранится телефон) —
        выборка по одной платформе теряла подписи остальных."""
        stmt = select(Subscriber.platform, Subscriber.user_id,
                      Subscriber.alias).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.alias.is_not(None),
        )
        rows = (await self.session.execute(stmt)).all()
        return {(p, uid): alias for p, uid, alias in rows}

    async def get_alias(self, platform: str, user_id: int) -> str | None:
        """Возвращает подпись участника от тренера, если задана."""
        stmt = select(Subscriber.alias).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.platform == platform,
            Subscriber.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def set_alias(self, platform: str, user_id: int,
                        alias: str | None) -> str | None:
        """
        Задаёт подпись участника от тренера (или снимает, если alias пустой).
        Подпись хранится ТОЛЬКО в профиле подписчика и видна лишь тренеру —
        в группу и общие списки она не попадает (там остаётся имя из Telegram).
        Возвращает имя, которое увидит тренер (подпись или имя из Telegram).
        """
        stmt = select(Subscriber).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.platform == platform,
            Subscriber.user_id == user_id,
        )
        sub = (await self.session.execute(stmt)).scalar_one_or_none()
        alias = (alias or "").strip() or None
        display = alias
        if sub:
            sub.alias = alias
            display = alias or sub.name
        await self.session.flush()
        return display

    async def list_participants(self) -> list[Subscriber]:
        """Все известные участники клуба (для управления/переименования)."""
        stmt = select(Subscriber).where(
            Subscriber.tenant_id == self.tenant_id,
        ).order_by(Subscriber.name)
        return list((await self.session.execute(stmt)).scalars())

    async def set_subscription(self, platform: str, user_id: int,
                               subscribed: bool) -> None:
        stmt = update(Subscriber).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.platform == platform,
            Subscriber.user_id == user_id,
        ).values(subscribed=subscribed)
        await self.session.execute(stmt)

    async def get_subscribers(self) -> list[Subscriber]:
        stmt = select(Subscriber).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.subscribed.is_(True),
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_subscriber(self, platform: str,
                             user_id: int) -> Subscriber | None:
        stmt = select(Subscriber).where(
            Subscriber.tenant_id == self.tenant_id,
            Subscriber.platform == platform,
            Subscriber.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---------- Статистика / посещаемость ----------

    async def user_stats(self, platform: str, user_id: int) -> dict:
        # посещённые тренировки + суммарные минуты + неоплаченные
        attended_stmt = (
            select(
                func.count().label("attended"),
                func.coalesce(func.sum(Training.duration_min), 0).label("minutes"),
                func.coalesce(
                    func.sum(case((Signup.paid.is_(False), 1), else_=0)), 0
                ).label("unpaid"),
            )
            .select_from(Signup)
            .join(Training, Training.id == Signup.training_id)
            .where(
                Signup.tenant_id == self.tenant_id,
                Signup.platform == platform,
                Signup.user_id == user_id,
                Signup.attended.is_(True),
                Training.is_cancelled.is_(False),
            )
        )
        row = (await self.session.execute(attended_stmt)).one()

        # прошедшие записи и пропуски
        past_stmt = (
            select(
                func.count().label("total"),
                func.coalesce(
                    func.sum(case((Signup.attended.is_(False), 1), else_=0)), 0
                ).label("missed"),
            )
            .select_from(Signup)
            .join(Training, Training.id == Signup.training_id)
            .where(
                Signup.tenant_id == self.tenant_id,
                Signup.platform == platform,
                Signup.user_id == user_id,
                Training.is_cancelled.is_(False),
                Training.state == "published",
                Training.start_at < _utcnow(),
            )
        )
        past = (await self.session.execute(past_stmt)).one()

        return {
            "attended": int(row.attended),
            "hours": round(int(row.minutes) / 60, 1),
            "unpaid": int(row.unpaid),
            "signups": int(past.total),
            "missed": int(past.missed),
        }

    async def attendance_ranking(self, limit: int = 15) -> list[dict]:
        stmt = (
            select(
                Signup.platform, Signup.user_id,
                func.max(Signup.name).label("name"),
                func.count().label("attended"),
                func.coalesce(func.sum(Training.duration_min), 0).label("minutes"),
            )
            .select_from(Signup)
            .join(Training, Training.id == Signup.training_id)
            .where(
                Signup.tenant_id == self.tenant_id,
                Signup.attended.is_(True),
                Training.is_cancelled.is_(False),
            )
            .group_by(Signup.platform, Signup.user_id)
            .order_by(func.count().desc(),
                      func.coalesce(func.sum(Training.duration_min), 0).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [{"name": r.name, "attended": int(r.attended),
                 "hours": round(int(r.minutes) / 60, 1)} for r in rows]

    async def list_debtors(self) -> list[dict]:
        stmt = (
            select(Signup.platform, Signup.user_id,
                   func.max(Signup.name).label("name"),
                   func.count().label("debts"))
            .select_from(Signup)
            .join(Training, Training.id == Signup.training_id)
            .where(
                Signup.tenant_id == self.tenant_id,
                Signup.attended.is_(True),
                Signup.paid.is_(False),
                Training.is_cancelled.is_(False),
            )
            .group_by(Signup.platform, Signup.user_id)
            .order_by(func.count().desc())
        )
        rows = (await self.session.execute(stmt)).all()
        result = []
        for r in rows:
            items_stmt = (
                select(Training.title, Training.start_at)
                .select_from(Signup)
                .join(Training, Training.id == Signup.training_id)
                .where(
                    Signup.tenant_id == self.tenant_id,
                    Signup.attended.is_(True),
                    Signup.paid.is_(False),
                    Training.is_cancelled.is_(False),
                    Signup.platform == r.platform,
                    Signup.user_id == r.user_id,
                )
                .order_by(Training.start_at.asc())
            )
            items = (await self.session.execute(items_stmt)).all()
            result.append({
                "platform": r.platform, "user_id": r.user_id, "name": r.name,
                "debts": int(r.debts),
                "items": [(it.title, it.start_at) for it in items],
            })
        return result

    # ---------- Outbox ----------

    async def enqueue(self, platform: str, user_id: int, text: str) -> None:
        if platform == "web":       # веб-участникам доставлять некуда
            return
        self.session.add(Outbox(
            tenant_id=self.tenant_id, platform=platform,
            user_id=user_id, text=text, sent=False,
        ))
        await self.session.flush()

    # ---------- Платежи ----------

    async def add_payment(self, **kwargs) -> Payment:
        p = Payment(tenant_id=self.tenant_id, **kwargs)
        self.session.add(p)
        await self.session.flush()
        return p

    async def get_payment_by_provider_id(self, provider_payment_id: str) -> Payment | None:
        stmt = select(Payment).where(
            Payment.tenant_id == self.tenant_id,
            Payment.provider_payment_id == provider_payment_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---------- Роли (memberships) ----------

    async def get_membership(self, tg_user_id: int) -> Membership | None:
        stmt = select(Membership).where(
            Membership.tenant_id == self.tenant_id,
            Membership.tg_user_id == tg_user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_membership(self, tg_user_id: int, role: str,
                                name: str = "") -> Membership:
        m = await self.get_membership(tg_user_id)
        if m:
            m.role = role
            if name:
                m.name = name
        else:
            m = Membership(tenant_id=self.tenant_id, tg_user_id=tg_user_id,
                          role=role, name=name)
            self.session.add(m)
        await self.session.flush()
        return m

    async def list_memberships(self) -> list[Membership]:
        stmt = select(Membership).where(Membership.tenant_id == self.tenant_id)
        return list((await self.session.execute(stmt)).scalars())


    # ─── регулярное расписание ───
    async def list_schedules(self) -> list[Schedule]:
        stmt = select(Schedule).where(
            Schedule.tenant_id == self.tenant_id
        ).order_by(Schedule.weekday, Schedule.time_str)
        return list((await self.session.execute(stmt)).scalars().all())

    async def add_schedule(self, **kwargs) -> Schedule:
        sch = Schedule(tenant_id=self.tenant_id, **kwargs)
        self.session.add(sch)
        await self.session.flush()
        return sch

    async def get_schedule(self, schedule_id: int) -> Schedule | None:
        stmt = select(Schedule).where(
            Schedule.id == schedule_id,
            Schedule.tenant_id == self.tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete_schedule(self, schedule_id: int) -> bool:
        stmt = select(Schedule).where(
            Schedule.id == schedule_id,
            Schedule.tenant_id == self.tenant_id)
        sch = (await self.session.execute(stmt)).scalar_one_or_none()
        if not sch:
            return False
        await self.session.delete(sch)
        return True


class GlobalRepository:
    """Запросы вне привязки к тенанту (управление тенантами, фоновые задачи)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_tenant(self, **kwargs) -> Tenant:
        t = Tenant(**kwargs)
        self.session.add(t)
        await self.session.flush()
        return t

    async def get_tenant(self, tenant_id: int) -> Tenant | None:
        return await self.session.get(Tenant, tenant_id)

    async def get_tenant_by_tg_chat(self, tg_chat_id: int) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.tg_chat_id == tg_chat_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_tenants(self) -> list[Tenant]:
        stmt = select(Tenant).where(Tenant.is_active.is_(True))
        return list((await self.session.execute(stmt)).scalars())

    # --- фоновые задачи across всех тенантов ---

    async def list_memberships_for_user(self, tg_user_id: int) -> list[Membership]:
        """Все роли пользователя во всех клубах (для входа в админку)."""
        stmt = select(Membership).where(Membership.tg_user_id == tg_user_id)
        return list((await self.session.execute(stmt)).scalars())

    async def get_payment_by_provider_id_global(
            self, provider_payment_id: str) -> Payment | None:
        stmt = select(Payment).where(
            Payment.provider_payment_id == provider_payment_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_payment_by_provider_id_global_for_update(
            self, provider_payment_id: str) -> Payment | None:
        """Как get_payment_by_provider_id_global, но блокирует строку
        (FOR UPDATE) — защита от гонки при двух почти одновременных
        вебхуках-ретраях провайдера (иначе оба могут пройти проверку
        status != 'succeeded' до commit друг друга). В SQLite no-op."""
        stmt = select(Payment).where(
            Payment.provider_payment_id == provider_payment_id
        ).with_for_update()
        try:
            return (await self.session.execute(stmt)).scalar_one_or_none()
        except Exception:
            return await self.get_payment_by_provider_id_global(provider_payment_id)

    async def fetch_pending_outbox(self, platform: str,
                                   limit: int = 50) -> list[Outbox]:
        """Только чтение — для инспекции состояния очереди (тесты, отладка).
        Реальная доставка (tasks.py) должна использовать claim_pending_outbox,
        которая захватывает записи атомарно перед отправкой."""
        stmt = select(Outbox).where(
            Outbox.platform == platform,
            Outbox.sent.is_(False),
        ).order_by(Outbox.id.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def claim_pending_outbox(self, platform: str,
                                   limit: int = 50) -> list[Outbox]:
        """Атомарно захватывает пачку неотправленных сообщений: сразу
        помечает их sent=True (UPDATE ... WHERE sent=False ... RETURNING),
        до того как реальная отправка вообще началась. Если приложение
        когда-нибудь запустят в нескольких экземплярах одновременно, каждое
        сообщение достанется только одному из них — второй UPDATE для тех
        же id не найдёт строк с sent=False и вернёт пустой список. Если
        отправка не удастся, вызывающий код возвращает sent обратно в False
        через record_outbox_failure — для повтора на следующем проходе."""
        subq = (
            select(Outbox.id)
            .where(Outbox.platform == platform, Outbox.sent.is_(False))
            .order_by(Outbox.id.asc())
            .limit(limit)
        )
        stmt = (
            update(Outbox)
            .where(Outbox.id.in_(subq))
            .values(sent=True)
            .returning(Outbox)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def mark_outbox_sent(self, outbox_id: int) -> None:
        await self.session.execute(
            update(Outbox).where(Outbox.id == outbox_id).values(sent=True)
        )

    async def record_outbox_failure(self, outbox_id: int, attempts: int) -> None:
        """Фиксирует неудачную попытку доставки и снимает захват (sent=False),
        чтобы сообщение снова стало доступно для повтора на следующем
        проходе очереди (см. claim_pending_outbox и tasks.py)."""
        await self.session.execute(
            update(Outbox).where(Outbox.id == outbox_id)
            .values(attempts=attempts, sent=False)
        )

    async def trainings_needing_reminder(self, window_min: int = 60) -> list[Training]:
        now = _utcnow()
        horizon = now + dt.timedelta(minutes=window_min)
        stmt = select(Training).where(
            Training.is_cancelled.is_(False),
            Training.state == "published",
            Training.reminder_sent.is_(False),
            Training.start_at > now,
            Training.start_at <= horizon,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def due_drafts(self) -> list[Training]:
        now = _utcnow()
        stmt = select(Training).where(
            Training.state == "draft",
            Training.is_cancelled.is_(False),
            Training.publish_at.is_not(None),
            Training.publish_at <= now,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def upcoming_published(self, within_hours: int = 24) -> list[Training]:
        """Опубликованные будущие тренировки в ближайшие N часов (для фон-задач)."""
        now = _utcnow()
        stmt = select(Training).where(
            Training.is_cancelled.is_(False),
            Training.state == "published",
            Training.start_at > now,
            Training.start_at <= now + dt.timedelta(hours=within_hours),
        )
        return list((await self.session.execute(stmt)).scalars())

    # --- отзывы о сервисе (публичная страница /reviews) ---

    async def add_review(self, *, name: str, club_name: str, rating: int,
                         text: str) -> Review:
        r = Review(name=name, club_name=club_name, rating=rating, text=text,
                   approved=False)
        self.session.add(r)
        await self.session.flush()
        return r

    async def list_approved_reviews(self, limit: int = 50) -> list[Review]:
        stmt = (select(Review).where(Review.approved.is_(True))
                .order_by(Review.created_at.desc()).limit(limit))
        return list((await self.session.execute(stmt)).scalars())

    async def list_pending_reviews(self) -> list[Review]:
        stmt = (select(Review).where(Review.approved.is_(False))
                .order_by(Review.created_at.asc()))
        return list((await self.session.execute(stmt)).scalars())

    async def get_review(self, review_id: int) -> Review | None:
        return await self.session.get(Review, review_id)

    async def set_review_approved(self, review_id: int, approved: bool) -> None:
        await self.session.execute(
            update(Review).where(Review.id == review_id).values(approved=approved)
        )

    async def delete_review(self, review_id: int) -> None:
        review = await self.session.get(Review, review_id)
        if review:
            await self.session.delete(review)
