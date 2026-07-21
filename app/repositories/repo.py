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
    ManageToken,
    Master,
    MasterReview,
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


# пауза перед повтором доставки по номеру попытки, в минутах
RETRY_DELAYS_MIN = (1, 2, 5, 15)


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

    # ---------- Мастера (салоны/тренеры) ----------

    async def list_masters(self, active_only: bool = True) -> list[Master]:
        stmt = select(Master).where(Master.tenant_id == self.tenant_id)
        if active_only:
            stmt = stmt.where(Master.active.is_(True))
        stmt = stmt.order_by(Master.id.asc())
        return list((await self.session.execute(stmt)).scalars())

    async def get_master(self, master_id: int) -> Master | None:
        stmt = select(Master).where(Master.id == master_id,
                                    Master.tenant_id == self.tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_master(self, *, name: str, specialty: str = "",
                         bio: str = "", photo_url: str | None = None) -> Master:
        m = Master(tenant_id=self.tenant_id, name=name, specialty=specialty,
                   bio=bio, photo_url=photo_url, active=True)
        self.session.add(m)
        await self.session.flush()
        return m

    async def deactivate_master(self, master_id: int) -> bool:
        """Скрывает мастера (active=False), не удаляя: у прошедших слотов
        сохраняется привязка для истории."""
        return await self.set_master_active(master_id, False)

    async def set_master_active(self, master_id: int, active: bool) -> bool:
        m = await self.get_master(master_id)
        if m is None:
            return False
        m.active = active
        return True

    async def masters_map(self) -> dict[int, Master]:
        """{id: Master} всех мастеров клуба (включая скрытых — для
        отображения у существующих слотов)."""
        stmt = select(Master).where(Master.tenant_id == self.tenant_id)
        return {m.id: m for m in (await self.session.execute(stmt)).scalars()}

    # ---------- Согласия ----------

    async def record_consent(self, *, platform: str, user_id: int | None,
                             purpose: str, consent_text: str) -> None:
        """Фиксирует факт согласия. Вызывается в ТОЙ ЖЕ транзакции, что и
        бизнес-действие: если запись не сохранилась, согласие тоже не
        считается данным — и наоборот."""
        from app.api.privacy_page import POLICY_VERSION
        from app.models.entities import ConsentEvent

        self.session.add(ConsentEvent(
            tenant_id=self.tenant_id, platform=platform, user_id=user_id,
            purpose=purpose, policy_version=POLICY_VERSION,
            consent_text=consent_text[:500]))
        await self.session.flush()

    # ---------- Веб-клиенты (телефон отдельно от идентификатора) ----------

    async def web_customer_id(self, phone: str, name: str = "") -> int:
        """id клиента по телефону — заводит запись при первом обращении.

        Наружу отдаётся суррогатный id: именно он попадает в signups и
        оценки. Сам номер лежит зашифрованным в web_customers."""
        from app.core import phones
        from app.models.entities import WebCustomer

        row = await self._find_web_customer(phone)
        if row is not None:
            if name and row.name != name:
                row.name = name[:200]
            return row.id
        enc, key_ver = phones.encrypt(phone)
        row = WebCustomer(tenant_id=self.tenant_id,
                          phone_index=phones.phone_index(phone),
                          phone_enc=enc, key_ver=key_ver,
                          index_ver=phones.active_key_ver(),
                          name=name[:200])
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def _find_web_customer(self, phone: str):
        """Клиент по телефону с учётом версий ключа индекса.

        Ищем индексом активной версии, затем индексами читаемых старых
        версий. Без этого добавление нового ключа «теряло» существующего
        клиента и на том же номере заводился дубль."""
        from app.core import phones
        from app.models.entities import WebCustomer

        for _ver, idx in phones.index_candidates(phone):
            stmt = select(WebCustomer).where(
                WebCustomer.tenant_id == self.tenant_id,
                WebCustomer.phone_index == idx)
            row = (await self.session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                return row
        return None

    async def find_web_customer_id(self, phone: str) -> int | None:
        """id по телефону без создания."""
        row = await self._find_web_customer(phone)
        return row.id if row is not None else None

    async def web_phones_map(self) -> dict[int, str]:
        """{user_id: расшифрованный телефон} по клубу — для карточки тренера,
        которому номер нужен, чтобы позвонить."""
        from app.core import phones
        from app.models.entities import WebCustomer

        stmt = select(WebCustomer.id, WebCustomer.phone_enc,
                      WebCustomer.key_ver).where(
            WebCustomer.tenant_id == self.tenant_id)
        return {cid: phones.decrypt(enc, ver)
                for cid, enc, ver in (await self.session.execute(stmt))}

    async def web_phone(self, user_id: int) -> str:
        from app.core import phones
        from app.models.entities import WebCustomer

        stmt = select(WebCustomer.phone_enc, WebCustomer.key_ver).where(
            WebCustomer.tenant_id == self.tenant_id,
            WebCustomer.id == user_id)
        row = (await self.session.execute(stmt)).first()
        return phones.decrypt(row[0], row[1]) if row else ""

    # ---------- Персональные ссылки управления ----------

    async def issue_manage_token(self, platform: str, user_id: int,
                                 token_hash: str, days: int = 90) -> ManageToken:
        """Регистрирует новую ссылку управления. Сам токен сюда не попадает
        — только его SHA-256: из базы восстановить ссылку нельзя.

        Прежние ссылки того же человека отзываются: иначе у клиента копится
        сколько угодно вечных ключей к своим данным, и потерянная год назад
        ссылка открывает их до сих пор. Действующей остаётся последняя —
        та, что человек только что получил."""
        await self.revoke_manage_tokens(platform, user_id)
        t = ManageToken(
            tenant_id=self.tenant_id, platform=platform, user_id=user_id,
            token_hash=token_hash,
            expires_at=_utcnow() + dt.timedelta(days=days))
        self.session.add(t)
        await self.session.flush()
        return t

    async def resolve_manage_token(self, token_hash: str) -> ManageToken | None:
        """Действующая (не отозванная, не истёкшая) ссылка или None."""
        stmt = select(ManageToken).where(
            ManageToken.tenant_id == self.tenant_id,
            ManageToken.token_hash == token_hash,
            ManageToken.revoked.is_(False),
            ManageToken.expires_at > _utcnow())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def purge_expired_manage_tokens(self) -> int:
        """Удаляет истёкшие и отозванные ссылки: держать их вечно незачем,
        а в резервных копиях они лишний след о клиентах."""
        from sqlalchemy import delete, or_

        res = await self.session.execute(
            delete(ManageToken).where(
                ManageToken.tenant_id == self.tenant_id,
                or_(ManageToken.expires_at < _utcnow(),
                    ManageToken.revoked.is_(True))))
        return res.rowcount or 0

    async def revoke_manage_tokens(self, platform: str, user_id: int) -> None:
        await self.session.execute(
            update(ManageToken)
            .where(ManageToken.tenant_id == self.tenant_id,
                   ManageToken.platform == platform,
                   ManageToken.user_id == user_id)
            .values(revoked=True)
            .execution_options(synchronize_session=False))

    async def forget_user(self, platform: str, user_id: int) -> dict[str, int]:
        """Удаляет персональные данные человека в этом клубе: записи,
        профиль с телефоном в подписи, оценки мастеров, ссылки управления.

        Возвращает, что именно удалено — человеку показываем результат, а не
        «готово» вслепую."""
        from sqlalchemy import delete

        from app.models.entities import WebCustomer

        removed = {}
        res = await self.session.execute(
            delete(WebCustomer).where(WebCustomer.tenant_id == self.tenant_id,
                                      WebCustomer.id == user_id))
        removed["web_customers"] = res.rowcount or 0
        for model in (Signup, MasterReview, Subscriber):
            res = await self.session.execute(
                delete(model).where(model.tenant_id == self.tenant_id,
                                    model.user_id == user_id,
                                    *([model.platform == platform]
                                      if hasattr(model, "platform") else [])))
            removed[model.__tablename__] = res.rowcount or 0
        await self.revoke_manage_tokens(platform, user_id)
        await self.session.flush()
        return removed

    # ---------- Рейтинг мастеров ----------

    async def has_visited_master(self, master_id: int, platform: str,
                                 user_id: int) -> bool:
        """Был ли у человека ПОДТВЕРЖДЁННЫЙ визит к этому мастеру.

        Мало того, что занятие прошло: человек мог записаться и не прийти.
        Раньше проверялось только время начала — значит, оценку мог
        поставить тот, кого мастер в глаза не видел. Теперь нужна отметка
        явки, которую ставит сам мастер или администратор."""
        now = dt.datetime.now(dt.timezone.utc)
        stmt = (select(func.count()).select_from(Signup)
                .join(Training, Training.id == Signup.training_id)
                .where(Signup.tenant_id == self.tenant_id,
                       Signup.platform == platform,
                       Signup.user_id == user_id,
                       Signup.status == "active",
                       Signup.attended.is_(True),
                       Training.master_id == master_id,
                       Training.start_at <= now))
        return bool((await self.session.execute(stmt)).scalar() or 0)

    async def upsert_master_review(self, *, master_id: int, user_id: int,
                                   author_name: str, rating: int,
                                   text: str = "") -> MasterReview:
        """Одна оценка на телефон: повторная — заменяет прежнюю."""
        stmt = select(MasterReview).where(
            MasterReview.tenant_id == self.tenant_id,
            MasterReview.master_id == master_id,
            MasterReview.user_id == user_id)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing:
            existing.rating = rating
            existing.text = text
            existing.author_name = author_name
            await self.session.flush()
            return existing
        r = MasterReview(tenant_id=self.tenant_id, master_id=master_id,
                         user_id=user_id, author_name=author_name,
                         rating=rating, text=text)
        self.session.add(r)
        await self.session.flush()
        return r

    async def master_rating_stats(self) -> dict[int, tuple[float, int]]:
        """{master_id: (средний балл, количество оценок)} по клубу."""
        stmt = (select(MasterReview.master_id,
                       func.avg(MasterReview.rating), func.count())
                .where(MasterReview.tenant_id == self.tenant_id)
                .group_by(MasterReview.master_id))
        rows = (await self.session.execute(stmt)).all()
        return {mid: (float(avg), cnt) for mid, avg, cnt in rows}

    async def list_master_reviews(self, master_id: int,
                                  limit: int = 5) -> list[MasterReview]:
        """Последние отзывы с текстом (пустые тексты не показываем)."""
        stmt = (select(MasterReview).where(
            MasterReview.tenant_id == self.tenant_id,
            MasterReview.master_id == master_id,
            MasterReview.text != "")
            .order_by(MasterReview.created_at.desc()).limit(limit))
        return list((await self.session.execute(stmt)).scalars())

    async def delete_master_review(self, review_id: int) -> bool:
        stmt = select(MasterReview).where(
            MasterReview.id == review_id,
            MasterReview.tenant_id == self.tenant_id)
        r = (await self.session.execute(stmt)).scalar_one_or_none()
        if r is None:
            return False
        await self.session.delete(r)
        return True

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
            Outbox.status == "pending",
        ).order_by(Outbox.id.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def requeue_stale_outbox(self, older_than_min: int = 10) -> int:
        """Возвращает в очередь сообщения, зависшие в processing.

        Захват держится только на время отправки. Если процесс убили
        посреди неё (деплой, перезапуск контейнера), запись осталась бы
        в processing навсегда и сообщение потерялось бы молча. Через
        older_than_min минут считаем захват протухшим и повторяем."""
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            minutes=older_than_min)
        result = await self.session.execute(
            update(Outbox)
            .where(Outbox.status == "processing", Outbox.claimed_at < cutoff)
            .values(status="pending", sent=False, claimed_at=None)
            # массовое обновление: не пересчитываем условие в Python по
            # объектам сессии (на SQLite там наивные даты — сравнение с
            # aware-границей падало бы с TypeError)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount or 0

    async def claim_pending_outbox(self, platform: str,
                                   limit: int = 50) -> list[Outbox]:
        """Атомарно захватывает пачку сообщений: переводит их из pending в
        processing (UPDATE ... WHERE status='pending' ... RETURNING) до того,
        как отправка вообще началась. Если приложение запустят в нескольких
        экземплярах, каждое сообщение достанется только одному: второй
        UPDATE для тех же id не найдёт строк в pending и вернёт пустой
        список.

        Дальше сообщение обязано прийти в одно из конечных состояний —
        sent или dead (см. mark_outbox_sent / mark_outbox_dead), либо
        вернуться в pending (record_outbox_failure). Зависшие в processing
        подбирает requeue_stale_outbox."""
        now = _utcnow()
        subq = (
            select(Outbox.id)
            .where(Outbox.platform == platform, Outbox.status == "pending",
                   # ещё не пришло время повтора — пропускаем
                   (Outbox.next_attempt_at.is_(None))
                   | (Outbox.next_attempt_at <= now))
            .order_by(Outbox.id.asc())
            .limit(limit)
        )
        # На PostgreSQL берём строки с пропуском заблокированных: два
        # экземпляра приложения не будут ждать друг друга на одной пачке.
        # SQLite (редакция Lite) блокировок строк не умеет — там достаточно
        # самого UPDATE ... RETURNING, он и так атомарен.
        if self.session.bind and self.session.bind.dialect.name != "sqlite":
            subq = subq.with_for_update(skip_locked=True)
        stmt = (
            update(Outbox)
            .where(Outbox.id.in_(subq))
            .values(status="processing", sent=True,
                    claimed_at=dt.datetime.now(dt.timezone.utc))
            .returning(Outbox)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def mark_outbox_sent(self, outbox_id: int) -> None:
        await self.session.execute(
            update(Outbox).where(Outbox.id == outbox_id)
            .values(sent=True, status="sent", claimed_at=None,
                    handled_at=_utcnow())
        )

    async def mark_outbox_dead(self, outbox_id: int, error: str = "") -> None:
        """Сообщение недоставляемо: попытки исчерпаны. Отдельное состояние,
        а не sent=True — иначе провал доставки неотличим от успеха и о нём
        никто никогда не узнает."""
        await self.session.execute(
            update(Outbox).where(Outbox.id == outbox_id)
            .values(sent=True, status="dead", claimed_at=None,
                    last_error=error[:300])
        )

    async def record_outbox_failure(self, outbox_id: int, attempts: int,
                                    error: str = "") -> None:
        """Фиксирует неудачную попытку и возвращает сообщение в очередь —
        но не раньше, чем через паузу: без неё заблокированный бот
        перебирался бы на каждом проходе и исчерпывал попытки за минуту."""
        idx = max(min(attempts, len(RETRY_DELAYS_MIN)) - 1, 0)
        await self.session.execute(
            update(Outbox).where(Outbox.id == outbox_id)
            .values(attempts=attempts, sent=False, status="pending",
                    claimed_at=None, last_error=error[:300],
                    next_attempt_at=_utcnow() + dt.timedelta(
                        minutes=RETRY_DELAYS_MIN[idx]))
        )

    async def list_dead_outbox(self, *, tenant_id: int | None = None,
                               platform: str = "", limit: int = 100):
        """Недоставленные сообщения для разбора оператором.

        Текст сообщения не отдаём: в нём имя и время занятия конкретного
        человека, а для разбора хватает клуба, платформы, числа попыток и
        причины."""
        stmt = select(Outbox).where(Outbox.status == "dead")
        if tenant_id:
            stmt = stmt.where(Outbox.tenant_id == tenant_id)
        if platform:
            stmt = stmt.where(Outbox.platform == platform)
        stmt = stmt.order_by(Outbox.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def retry_dead_outbox(self, outbox_id: int) -> bool:
        """Вернуть недоставленное в очередь: счётчик попыток обнуляем,
        иначе оно тут же снова упадёт в dead."""
        res = await self.session.execute(
            update(Outbox)
            .where(Outbox.id == outbox_id, Outbox.status == "dead")
            .values(status="pending", sent=False, attempts=0,
                    claimed_at=None, next_attempt_at=None, handled_at=None))
        return bool(res.rowcount)

    async def discard_dead_outbox(self, outbox_id: int) -> bool:
        """Отбросить: сообщение больше не нужно (клуб ушёл, чат удалён)."""
        res = await self.session.execute(
            update(Outbox)
            .where(Outbox.id == outbox_id, Outbox.status == "dead")
            .values(status="discarded", sent=True, handled_at=_utcnow()))
        return bool(res.rowcount)

    async def outbox_health(self) -> dict[str, int]:
        """Сводка для алерта: сколько недоставленных и как давно ждёт
        самое старое сообщение в очереди (в минутах)."""
        dead = await self.count_dead_outbox()
        oldest = (await self.session.execute(
            select(func.min(Outbox.created_at)).where(
                Outbox.status == "pending"))).scalar()
        age_min = 0
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=dt.timezone.utc)
            age_min = int((_utcnow() - oldest).total_seconds() // 60)
        return {"dead": dead, "pending_age_min": max(age_min, 0)}

    async def count_dead_outbox(self) -> int:
        """Сколько сообщений так и не доставлено — для отчёта владельцу."""
        stmt = select(func.count()).select_from(Outbox).where(
            Outbox.status == "dead")
        return int((await self.session.execute(stmt)).scalar() or 0)

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

    async def record_platform_consent(self, *, purpose: str,
                                      consent_text: str) -> None:
        """Согласие на общеплатформенной форме: клуба у неё нет, поэтому
        tenant_id пустой. Субъект не сохраняем — на этой форме нет ни
        телефона, ни аккаунта, только имя, которое человек указал сам."""
        from app.api.privacy_page import POLICY_VERSION
        from app.models.entities import ConsentEvent

        self.session.add(ConsentEvent(
            tenant_id=None, platform="web", user_id=None, purpose=purpose,
            policy_version=POLICY_VERSION, consent_text=consent_text[:500],
            source="platform-form"))
        await self.session.flush()

    async def get_state(self, key: str) -> str:
        from app.models.entities import PlatformState
        row = await self.session.get(PlatformState, key)
        return row.value if row else ""

    async def set_state(self, key: str, value: str) -> None:
        from app.models.entities import PlatformState
        row = await self.session.get(PlatformState, key)
        if row:
            row.value = value[:300]
            row.updated_at = _utcnow()
        else:
            self.session.add(PlatformState(key=key, value=value[:300]))
        await self.session.flush()

    async def demo_tenant_id(self) -> int | None:
        """Клуб-витрина (Tenant.is_demo), на который можно спокойно послать
        любого посетителя. None — если демо-клуба нет."""
        stmt = (select(Tenant.id).where(Tenant.is_demo.is_(True),
                                        Tenant.is_active.is_(True))
                .order_by(Tenant.id.asc()).limit(1))
        return (await self.session.execute(stmt)).scalar_one_or_none()

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
