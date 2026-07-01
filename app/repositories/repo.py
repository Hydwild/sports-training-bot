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
        Обновляет отображаемое имя во всех его записях. Возвращает
        актуальное отображаемое имя.
        """
        # обновляем подписчика
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
        # применяем имя ко всем записям этого участника (не гостям)
        if display:
            upd = update(Signup).where(
                Signup.tenant_id == self.tenant_id,
                Signup.platform == platform,
                Signup.user_id == user_id,
                Signup.is_guest.is_(False),
            ).values(name=display)
            await self.session.execute(upd)
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

    async def fetch_pending_outbox(self, platform: str,
                                   limit: int = 50) -> list[Outbox]:
        stmt = select(Outbox).where(
            Outbox.platform == platform,
            Outbox.sent.is_(False),
        ).order_by(Outbox.id.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def mark_outbox_sent(self, outbox_id: int) -> None:
        await self.session.execute(
            update(Outbox).where(Outbox.id == outbox_id).values(sent=True)
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
