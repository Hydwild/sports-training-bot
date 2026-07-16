"""
Сервисный слой: бизнес-логика записи, очереди, посещаемости.
Не знает ни про Telegram/VK, ни про HTTP — только про репозиторий.
Работает в пределах одного тенанта.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Signup, Training
from app.repositories.repo import TenantRepository


@dataclass
class SignupResult:
    result: str            # active | queue | already | closed
    position: int = 0
    status: str = ""


class BookingService:
    def __init__(self, session: AsyncSession, tenant_id: int,
                 tz: str = "Europe/Moscow") -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.repo = TenantRepository(session, tenant_id)
        self.tz = ZoneInfo(tz)

    # ---------- Запись ----------

    async def sign_up(self, training_id: int, platform: str,
                      user_id: int, name: str,
                      username: str | None = None) -> SignupResult:
        training = await self.repo.get_training_for_update(training_id)
        if not training or training.is_cancelled or training.state != "published":
            return SignupResult("closed")

        # автозакрытие записи за N минут до начала (настройка клуба)
        from app.models.entities import Tenant
        tenant = await self.session.get(Tenant, self.tenant_id)
        if tenant and tenant.signup_close_minutes:
            start = (training.start_at if training.start_at.tzinfo
                     else training.start_at.replace(tzinfo=dt.timezone.utc))
            left = (start - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
            if left <= tenant.signup_close_minutes:
                return SignupResult("closed")

        existing = await self.repo.get_user_signup(training_id, platform, user_id)
        if existing:
            return SignupResult("already", existing.position, existing.status)

        status, position = await self._place(training)
        # в записи храним обычное имя из Telegram (для группы и общих списков);
        # подпись тренера применяется только при показе тренеру
        await self.repo.add_signup(
            training_id=training_id, platform=platform, user_id=user_id,
            name=name, username=username, status=status, position=position,
        )
        await self.session.commit()
        return SignupResult(status, position)

    async def sign_up_guest(self, training_id: int, guest_name: str,
                            added_by: int) -> SignupResult:
        """
        Записать гостя (человека без доступа к сети) за другого участника.
        Запись помечается неподтверждённой (confirmed=False) — тренер потом
        подтверждает или отклоняет её. Место занимается по общим правилам.
        """
        training = await self.repo.get_training_for_update(training_id)
        if not training or training.is_cancelled or training.state != "published":
            return SignupResult("closed")

        # синтетический случайный id для гостя. Namespace изолирован полем
        # platform="guest" (не пересекается с реальными tg/vk id), но сам id
        # обязан быть уникален в пределах (tenant_id, training_id, platform) —
        # уникальный индекс signups. Раньше брали время в мс, что могло
        # столкнуться при двойном тапе/ретрае сети в одну и ту же миллисекунду
        # (и приводило к необработанному IntegrityError). Случайные 31 бит
        # делают коллизию практически невозможной без завязки на время.
        import secrets
        guest_uid = secrets.randbits(31)
        status, position = await self._place(training)
        await self.repo.add_signup(
            training_id=training_id, platform="guest", user_id=guest_uid,
            name=guest_name, status=status, position=position,
            is_guest=True, confirmed=False, added_by=added_by,
        )
        await self.session.commit()
        return SignupResult(status, position, status)

    async def _place(self, training) -> tuple[str, int]:
        """Определяет статус (active/queue) и позицию для новой записи."""
        active = await self.repo.get_signups(training.id, "active")
        queue = await self.repo.get_signups(training.id, "queue")
        if len(active) < training.max_participants:
            return "active", (active[-1].position + 1) if active else 1
        return "queue", (queue[-1].position + 1) if queue else 1

    async def confirm_guest(self, signup_id: int) -> Signup | None:
        """Тренер подтверждает гостевую запись как реально занятую."""
        s = await self.repo.get_signup_by_id(signup_id)
        if s and s.is_guest:
            s.confirmed = True
            await self.session.commit()
        return s

    async def reject_guest(self, signup_id: int) -> dict:
        """
        Тренер отклоняет гостевую запись: место освобождается, первый из
        очереди поднимается (как при обычной отмене).
        """
        s = await self.repo.get_signup_by_id(signup_id)
        if not s or not s.is_guest:
            return {"rejected": False, "promoted": None}
        was_active = s.status == "active"
        name = s.name
        await self.repo.delete_signup(s)
        promoted = None
        if was_active:
            promoted_list = await self._rebalance(s.training_id)
            promoted = promoted_list[0] if promoted_list else None
        else:
            await self._renumber_queue(s.training_id)
        await self.session.commit()
        return {"rejected": True, "promoted": promoted, "name": name}

    async def list_unconfirmed_guests(self, training_id: int) -> list[Signup]:
        signups = (await self.repo.get_signups(training_id, "active")
                   + await self.repo.get_signups(training_id, "queue"))
        return [s for s in signups if s.is_guest and not s.confirmed]

    async def cancel_signup(self, training_id: int, platform: str,
                            user_id: int, lock_minutes: int = 0) -> dict:
        # блокируем строку тренировки — защита от гонок при отмене/rebalance
        await self.repo.get_training_for_update(training_id)
        signup = await self.repo.get_user_signup(training_id, platform, user_id)
        if not signup:
            return {"cancelled": False, "promoted": None}

        # окно отмены: запрещаем отписку, если до начала меньше lock_minutes
        if lock_minutes > 0:
            training = await self.repo.get_training(training_id)
            if training:
                start = training.start_at
                if start.tzinfo is None:
                    start = start.replace(tzinfo=dt.timezone.utc)
                left = (start - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
                if 0 <= left < lock_minutes:
                    return {"cancelled": False, "promoted": None, "locked": True,
                            "lock_minutes": lock_minutes}

        was_active = signup.status == "active"
        await self.repo.delete_signup(signup)

        promoted = None
        if was_active:
            promoted_list = await self._rebalance(training_id)
            promoted = promoted_list[0] if promoted_list else None
        else:
            await self._renumber_queue(training_id)

        await self.session.commit()
        return {"cancelled": True, "promoted": promoted}

    async def set_max_participants(self, training_id: int,
                                   new_max: int) -> list[Signup]:
        # блокируем строку — rebalance меняет состав, защита от гонок
        training = await self.repo.get_training_for_update(training_id)
        if not training:
            return []
        training.max_participants = new_max
        await self.session.flush()
        promoted = await self._rebalance(training_id)
        await self.session.commit()
        return promoted

    async def _rebalance(self, training_id: int) -> list[Signup]:
        """Поднимаем из очереди в активные, пока есть места. Кладём уведомления."""
        training = await self.repo.get_training(training_id)
        if not training:
            return []
        active = await self.repo.get_signups(training_id, "active")
        free = training.max_participants - len(active)
        promoted: list[Signup] = []
        if free > 0:
            queue = await self.repo.get_signups(training_id, "queue")
            max_pos = max((s.position for s in active), default=0)
            for i, s in enumerate(queue[:free], start=1):
                s.status = "active"
                s.position = max_pos + i
                promoted.append(s)
            await self.session.flush()
        await self._renumber_queue(training_id)

        when = self.format_local(training.start_at)
        for p in promoted:
            await self.repo.enqueue(
                p.platform, p.user_id,
                f"🎉 Освободилось место! Вы в основном составе на "
                f"«{training.title}» ({when}).",
            )
        return promoted

    async def _renumber_queue(self, training_id: int) -> None:
        queue = await self.repo.get_signups(training_id, "queue")
        for i, s in enumerate(queue, start=1):
            s.position = i
        await self.session.flush()

    # ---------- Тренировки ----------

    async def common_values(self) -> dict:
        """
        Частые значения для подсказок при создании: лимит участников и
        длительность (берём самые используемые из истории клуба).
        """
        from sqlalchemy import select, func
        from app.models.entities import Training
        # самые частые лимиты
        max_stmt = (select(Training.max_participants, func.count().label("c"))
                    .where(Training.tenant_id == self.tenant_id)
                    .group_by(Training.max_participants)
                    .order_by(func.count().desc()).limit(4))
        maxes = [row[0] for row in (await self.session.execute(max_stmt)).all()]
        # самые частые длительности
        dur_stmt = (select(Training.duration_min, func.count().label("c"))
                    .where(Training.tenant_id == self.tenant_id)
                    .group_by(Training.duration_min)
                    .order_by(func.count().desc()).limit(4))
        durs = [row[0] for row in (await self.session.execute(dur_stmt)).all()]
        # самые частые цены (в копейках)
        price_stmt = (select(Training.price_minor, func.count().label("c"))
                      .where(Training.tenant_id == self.tenant_id)
                      .group_by(Training.price_minor)
                      .order_by(func.count().desc()).limit(4))
        prices = [row[0] for row in (await self.session.execute(price_stmt)).all()]
        return {"max": maxes, "dur": durs, "prices": prices}

    async def recent_locations(self, limit: int = 4) -> list[str]:
        """Недавно использованные места (для быстрых кнопок при создании)."""
        from sqlalchemy import select
        from app.models.entities import Training
        stmt = (select(Training.location)
                .where(Training.tenant_id == self.tenant_id,
                       Training.location != "")
                .order_by(Training.id.desc()))
        rows = (await self.session.execute(stmt)).scalars().all()
        seen, result = set(), []
        for loc in rows:
            if loc not in seen:
                seen.add(loc); result.append(loc)
            if len(result) >= limit:
                break
        return result

    async def times_for_weekday(self, weekday: int, limit: int = 3) -> list[str]:
        """
        Времена (ЧЧ:ММ) прошлых тренировок в указанный день недели
        (0=Пн ... 6=Вс) — для подсказок «как обычно в этот день».
        """
        from sqlalchemy import select
        from app.models.entities import Training
        stmt = (select(Training.start_at)
                .where(Training.tenant_id == self.tenant_id)
                .order_by(Training.id.desc()))
        rows = (await self.session.execute(stmt)).scalars().all()
        seen, result = set(), []
        for start in rows:
            local = start.astimezone(self.tz) if start.tzinfo else start
            if local.weekday() != weekday:
                continue
            hhmm = local.strftime("%H:%M")
            if hhmm not in seen:
                seen.add(hhmm); result.append(hhmm)
            if len(result) >= limit:
                break
        return result

    async def create_training(self, *, title: str, start_at: dt.datetime,
                              location: str, max_participants: int,
                              platform: str, user_id: int,
                              duration_min: int = 120, state: str = "published",
                              publish_at: dt.datetime | None = None) -> Training:
        training = await self.repo.add_training(
            title=title, start_at=start_at, location=location,
            max_participants=max_participants, duration_min=duration_min,
            state=state, publish_at=publish_at,
            created_by_platform=platform, created_by_id=user_id,
        )
        await self.session.commit()
        return training

    async def my_trainings(self, platform: str, user_id: int) -> list:
        """Все предстоящие тренировки, куда записан юзер (active и queue),
        вместе со статусом записи. Возвращает список (Training, status, position)."""
        from sqlalchemy import select
        from app.models.entities import Training, Signup
        now = dt.datetime.now(dt.timezone.utc)
        stmt = (select(Training, Signup.status, Signup.position)
                .join(Signup, Signup.training_id == Training.id)
                .where(Training.tenant_id == self.tenant_id,
                       Training.is_cancelled.is_(False),
                       Training.start_at > now,
                       Signup.platform == platform,
                       Signup.user_id == user_id,
                       Signup.status.in_(("active", "queue")))
                .order_by(Training.start_at.asc()))
        rows = (await self.session.execute(stmt)).all()
        return [(r[0], r[1], r[2]) for r in rows]

    async def next_training_for_user(self, platform: str, user_id: int):
        """Ближайшая будущая тренировка, на которую записан пользователь."""
        from sqlalchemy import select
        from app.models.entities import Training, Signup
        now = dt.datetime.now(dt.timezone.utc)
        stmt = (select(Training)
                .join(Signup, Signup.training_id == Training.id)
                .where(Training.tenant_id == self.tenant_id,
                       Training.is_cancelled.is_(False),
                       Training.start_at > now,
                       Signup.platform == platform,
                       Signup.user_id == user_id,
                       Signup.status == "active")
                .order_by(Training.start_at.asc()))
        return (await self.session.execute(stmt)).scalars().first()

    async def update_field(self, training_id: int, field: str, value) -> Training | None:
        """Редактирование одного поля тренировки (time/location/maxp/duration)."""
        if field == "max_participants":
            # отдельный метод блокирует строку тренировки (FOR UPDATE) перед
            # rebalance — важно при гонке с одновременной записью участника.
            await self.set_max_participants(training_id, value)
            return await self.repo.get_training(training_id)
        training = await self.repo.get_training(training_id)
        if not training:
            return None
        if field == "start_at":
            training.start_at = value
        elif field == "location":
            training.location = value
        elif field == "duration_min":
            training.duration_min = value
        elif field == "price_minor":
            training.price_minor = value
        elif field == "title":
            training.title = value
        await self.session.commit()
        return training

    async def repeat_training(self, training_id: int,
                              days_ahead: int = 7) -> Training | None:
        """Создаёт копию тренировки со сдвигом даты (по умолчанию +7 дней)."""
        src = await self.repo.get_training(training_id)
        if not src:
            return None
        new_start = src.start_at + dt.timedelta(days=days_ahead)
        return await self.create_training(
            title=src.title, start_at=new_start, location=src.location,
            max_participants=src.max_participants, duration_min=src.duration_min,
            state="published", publish_at=None, platform="api", user_id=0,
        )

    async def notify_changed(self, training_id: int, change_desc: str) -> int:
        """Уведомляет всех записанных (active+queue) об изменении тренировки.
        change_desc — что именно изменилось. Возвращает число адресатов."""
        training = await self.repo.get_training(training_id)
        if not training:
            return 0
        participants = (await self.repo.get_signups(training_id, "active")
                        + await self.repo.get_signups(training_id, "queue"))
        when = self.format_local(training.start_at)
        text = (f"✏️ Изменение в тренировке «{training.title}»:\n{change_desc}\n\n"
                f"📅 Сейчас: {when}"
                + (f"\n📍 {training.location}" if training.location else ""))
        for s in participants:
            if getattr(s, "is_guest", False):
                continue
            await self.repo.enqueue(s.platform, s.user_id, text)
        await self.session.commit()
        return len(participants)

    async def cancel_training(self, training_id: int) -> None:
        training = await self.repo.get_training(training_id)
        if not training:
            return
        participants = (await self.repo.get_signups(training_id, "active")
                        + await self.repo.get_signups(training_id, "queue"))
        training.is_cancelled = True
        await self.session.flush()
        when = self.format_local(training.start_at)
        for s in participants:
            await self.repo.enqueue(
                s.platform, s.user_id,
                f"⚠️ Тренировка «{training.title}» ({when}) отменена.",
            )
        await self.session.commit()

    async def publish_training(self, training_id: int,
                               notify: bool = True) -> Training | None:
        training = await self.repo.get_training(training_id)
        if not training:
            return None
        training.state = "published"
        training.publish_at = None
        await self.session.flush()
        if notify:
            subs = await self.repo.get_subscribers()
            when = self.format_local(training.start_at)
            text = (f"🏸 Открыта запись на «{training.title}»\n📅 {when}"
                    + (f"\n📍 {training.location}" if training.location else ""))
            for sub in subs:
                await self.repo.enqueue(sub.platform, sub.user_id, text)
        await self.session.commit()
        return training

    # ---------- Посещаемость / оплата ----------

    async def toggle_attended(self, signup_id: int) -> Signup | None:
        s = await self.repo.get_signup_by_id(signup_id)
        if s:
            s.attended = not s.attended
            await self.session.commit()
        return s

    async def toggle_paid(self, signup_id: int) -> Signup | None:
        s = await self.repo.get_signup_by_id(signup_id)
        if s:
            s.paid = not s.paid
            await self.session.commit()
        return s

    # ---------- Статистика / должники / рассылки / экспорт ----------

    async def user_stats(self, platform: str, user_id: int) -> dict:
        return await self.repo.user_stats(platform, user_id)

    async def monthly_summary(self, months: int = 3) -> list[dict]:
        """Сводка по месяцам: сколько тренировок и посещений.
        Возвращает список {month: 'ГГГГ-ММ', trainings, attended}, свежие сверху."""
        from collections import defaultdict
        from app.models.entities import Training, Signup
        from sqlalchemy import select
        now = dt.datetime.now(dt.timezone.utc)
        trs = list((await self.session.execute(
            select(Training).where(
                Training.tenant_id == self.tenant_id,
                Training.is_cancelled.is_(False),
                Training.state == "published",
                Training.start_at < now))).scalars())
        by_month = defaultdict(lambda: {"trainings": 0, "attended": 0})
        month_by_tid: dict[int, str] = {}
        for t in trs:
            local = self.format_local(t.start_at)  # ДД.ММ.ГГГГ ЧЧ:ММ
            d, m, y = local.split(" ")[0].split(".")
            key = f"{y}-{m}"
            by_month[key]["trainings"] += 1
            month_by_tid[t.id] = key
        # одним запросом вместо запроса на каждый месяц (N+1)
        if month_by_tid:
            attended_tids = (await self.session.execute(
                select(Signup.training_id).where(
                    Signup.tenant_id == self.tenant_id,
                    Signup.training_id.in_(month_by_tid.keys()),
                    Signup.attended.is_(True)))).scalars().all()
            for tid in attended_tids:
                key = month_by_tid.get(tid)
                if key:
                    by_month[key]["attended"] += 1
        rows = [{"month": k, **v} for k, v in by_month.items()]
        rows.sort(key=lambda r: r["month"], reverse=True)
        return rows[:months]

    async def attendance_ranking(self, limit: int = 15) -> list[dict]:
        return await self.repo.attendance_ranking(limit)

    async def training_attendance(self, training_id: int) -> dict:
        active = await self.repo.get_signups(training_id, "active")
        attended = [s for s in active if s.attended]
        paid = [s for s in attended if s.paid]
        return {"signed": len(active), "attended": len(attended),
                "paid": len(paid), "unpaid": len(attended) - len(paid)}

    async def list_debtors(self) -> list[dict]:
        return await self.repo.list_debtors()

    async def remind_debtors(self) -> int:
        debtors = await self.repo.list_debtors()
        for d in debtors:
            lines = [f"💰 Напоминание об оплате. За вами {d['debts']} "
                     f"неоплаченных тренировок:"]
            for title, when in d["items"]:
                lines.append(f"  • {title} ({self.format_local(when)})")
            lines.append("\nПожалуйста, погасите задолженность. Спасибо!")
            await self.repo.enqueue(d["platform"], d["user_id"], "\n".join(lines))
        await self.session.commit()
        return len(debtors)

    async def broadcast(self, text: str) -> dict:
        subs = await self.repo.get_subscribers()
        for sub in subs:
            await self.repo.enqueue(sub.platform, sub.user_id, f"📢 {text}")
        await self.session.commit()
        return {"tg": sum(1 for s in subs if s.platform == "tg"),
                "vk": sum(1 for s in subs if s.platform == "vk")}

    async def export_rows(self, training_id: int) -> tuple | None:
        """Возвращает (training, rows) для Excel/PDF экспортеров, или None."""
        training = await self.repo.get_training(training_id)
        if not training:
            return None
        active = await self.repo.get_signups(training_id, "active")
        queue = await self.repo.get_signups(training_id, "queue")
        rows = []
        for i, s in enumerate(active, 1):
            rows.append({"n": i, "status": "записан", "name": s.name,
                         "platform": s.platform,
                         "attended": "да" if s.attended else "нет",
                         "paid": "да" if s.paid else "нет"})
        for i, s in enumerate(queue, 1):
            rows.append({"n": i, "status": "очередь", "name": s.name,
                         "platform": s.platform, "attended": "", "paid": ""})
        return training, rows

    async def export_training_csv(self, training_id: int) -> str | None:
        import csv, io
        training = await self.repo.get_training(training_id)
        if not training:
            return None
        active = await self.repo.get_signups(training_id, "active")
        queue = await self.repo.get_signups(training_id, "queue")
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Тренировка", training.title])
        w.writerow(["Дата", self.format_local(training.start_at)])
        w.writerow(["Место", training.location or "-"])
        w.writerow(["Лимит", training.max_participants])
        w.writerow([])
        w.writerow(["№", "Статус", "Имя", "Платформа", "Пришёл", "Оплатил"])
        for i, s in enumerate(active, 1):
            w.writerow([i, "записан", s.name, s.platform,
                        "да" if s.attended else "нет", "да" if s.paid else "нет"])
        for i, s in enumerate(queue, 1):
            w.writerow([i, "очередь", s.name, s.platform, "", ""])
        return buf.getvalue()

    # ---------- Утилиты дат ----------

    def parse_local(self, text: str) -> dt.datetime | None:
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
            try:
                naive = dt.datetime.strptime(text.strip(), fmt)
                return naive.replace(tzinfo=self.tz).astimezone(dt.timezone.utc)
            except ValueError:
                continue
        return None

    def format_local(self, when: dt.datetime) -> str:
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return when.astimezone(self.tz).strftime("%d.%m.%Y %H:%M")

    # ─────────── Демо-наполнение (для показа клиентам) ───────────
    async def seed_demo(self) -> bool:
        """Наполняет пустой клуб примером: тренировки, записи, явка,
        расписание. Возвращает False, если данные уже есть."""
        import random
        if await self.repo.list_upcoming():
            return False
        now = dt.datetime.now(dt.timezone.utc)
        names = ["Андрей", "Мария", "Сергей", "Ольга", "Дмитрий",
                 "Анна", "Павел", "Ирина"]
        # прошедшая тренировка с явкой — для рейтинга и статистики
        past = await self.create_training(
            title="Вечерняя игра", start_at=now - dt.timedelta(days=3),
            location="Зал «Олимп»", max_participants=8, duration_min=90,
            state="published", publish_at=None, platform="api", user_id=0)
        for i, n in enumerate(names[:6]):
            await self.repo.upsert_subscriber("demo", 900100 + i, n)
            await self.sign_up(past.id, "demo", 900100 + i, n)
        for i, s in enumerate(await self.repo.get_signups(past.id, "active")):
            s.attended = (i == 0) or random.random() > 0.25
        # предстоящие
        plans = [("Игровая тренировка", 1, "19:00", 8, 500),
                 ("Тренировка для новичков", 3, "18:00", 6, 400),
                 ("Турнир выходного дня", 5, "11:00", 12, 700)]
        for title, days, hhmm, mx, price in plans:
            d = (now + dt.timedelta(days=days)).astimezone(self.tz).date()
            tr = await self.create_training(
                title=title,
                start_at=self.parse_local(
                    f"{d.day:02d}.{d.month:02d}.{d.year} {hhmm}"),
                location="Зал «Олимп»", max_participants=mx,
                duration_min=90, state="published", publish_at=None,
                platform="api", user_id=0)
            tr.price_minor = price * 100
            for i, n in enumerate(random.sample(names, k=min(3, mx))):
                await self.sign_up(tr.id, "demo", 900200 + i, n)
        # регулярное расписание
        await self.repo.add_schedule(
            weekday=1, time_str="19:00", title="Игровая тренировка",
            location="Зал «Олимп»", duration_min=90, price_minor=50000,
            max_participants=8, days_ahead=3)
        await self.session.commit()
        return True
