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
        training = await self.repo.get_training(training_id)
        if not training or training.is_cancelled or training.state != "published":
            return SignupResult("closed")

        existing = await self.repo.get_user_signup(training_id, platform, user_id)
        if existing:
            return SignupResult("already", existing.position, existing.status)

        status, position = await self._place(training)
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
        training = await self.repo.get_training(training_id)
        if not training or training.is_cancelled or training.state != "published":
            return SignupResult("closed")

        # синтетический отрицательный id, чтобы не конфликтовать с реальными
        import time
        guest_uid = -int(time.time() * 1000) % 1_000_000_000
        status, position = await self._place(training)
        s = await self.repo.add_signup(
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
        training = await self.repo.get_training(training_id)
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
