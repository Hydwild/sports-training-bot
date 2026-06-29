"""
Фоновые задачи уровня площадки (по всем тенантам):
  - доставка уведомлений из outbox в Telegram/VK,
  - напоминания о тренировках,
  - авто-публикация черновиков по таймеру.

Реальная отправка делегируется «отправителям» (senders), которые
регистрирует слой ботов. Если отправитель не задан (например, VK выключен),
сообщения этой платформы пропускаются.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from app.db.engine import SessionLocal
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService

logger = logging.getLogger("tasks")

# platform -> async функция (user_id, text) -> None
Sender = Callable[[int, str], Awaitable[None]]
_senders: dict[str, Sender] = {}


def register_sender(platform: str, sender: Sender) -> None:
    _senders[platform] = sender


async def deliver_outbox_loop() -> None:
    while True:
        try:
            await _deliver_once()
        except Exception:
            logger.exception("Ошибка доставки outbox")
        await asyncio.sleep(2)


async def _deliver_once() -> None:
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        for platform, sender in _senders.items():
            pending = await g.fetch_pending_outbox(platform, limit=25)
            for item in pending:
                try:
                    await sender(item.user_id, item.text)
                except Exception:
                    logger.warning("Доставка %s user=%s не удалась",
                                   platform, item.user_id)
                # помечаем отправленным в любом случае, чтобы не зациклиться
                await g.mark_outbox_sent(item.id)
            await session.commit()


async def scheduler_loop() -> None:
    while True:
        try:
            await _run_scheduler()
        except Exception:
            logger.exception("Ошибка планировщика")
        await asyncio.sleep(60)


async def _run_scheduler() -> None:
    import datetime as dt
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        now = dt.datetime.now(dt.timezone.utc)

        # кэш настроек клубов, чтобы не дёргать БД повторно
        tenant_cache: dict[int, object] = {}

        async def tenant_of(tid: int):
            if tid not in tenant_cache:
                tenant_cache[tid] = await g.get_tenant(tid)
            return tenant_cache[tid]

        # обходим все опубликованные будущие тренировки в ближайшие сутки
        for training in await g.upcoming_published(within_hours=24):
            tenant = await tenant_of(training.tenant_id)
            if tenant is None:
                continue
            svc = BookingService(session, training.tenant_id, tz=tenant.timezone)
            minutes_left = (training.start_at - now).total_seconds() / 60
            when = svc.format_local(training.start_at)

            # 1) напоминание участникам
            if (tenant.reminder_enabled and not training.reminder_sent
                    and minutes_left <= tenant.reminder_minutes):
                for s in await svc.repo.get_signups(training.id, "active"):
                    await svc.repo.enqueue(
                        s.platform, s.user_id,
                        f"⏰ Скоро тренировка «{training.title}» в {when}"
                        + (f", {training.location}." if training.location else "."))
                training.reminder_sent = True

            # 2) напоминание тренеру о неподтверждённых гостях
            if (tenant.guest_reminder_minutes > 0 and not training.guest_reminder_sent
                    and minutes_left <= tenant.guest_reminder_minutes):
                guests = await svc.list_unconfirmed_guests(training.id)
                if guests and tenant.admin_tg_id:
                    names = ", ".join(x.name for x in guests)
                    await svc.repo.enqueue(
                        "tg", tenant.admin_tg_id,
                        f"⏳ «{training.title}» ({when}): неподтверждённые гости — "
                        f"{names}. Подтвердите или отклоните: /guests")
                training.guest_reminder_sent = True

            # 3) авто-истечение неподтверждённых гостей
            if (tenant.guest_expire_enabled and not training.guests_expired
                    and minutes_left <= tenant.guest_expire_minutes):
                guests = await svc.list_unconfirmed_guests(training.id)
                for guest in guests:
                    res = await svc.reject_guest(guest.id)
                    if res.get("promoted"):
                        # уведомление поднятому уже кладётся в reject_guest/_rebalance
                        pass
                if guests:
                    # уведомим тренера об автоосвобождении
                    if tenant.admin_tg_id:
                        await svc.repo.enqueue(
                            "tg", tenant.admin_tg_id,
                            f"♻️ «{training.title}»: {len(guests)} неподтверждённых "
                            f"гостей автоматически сняты, места освобождены.")
                training.guests_expired = True

        await session.commit()

        # авто-публикация черновиков (с учётом настройки уведомления)
        for training in await g.due_drafts():
            tenant = await tenant_of(training.tenant_id)
            svc = BookingService(session, training.tenant_id,
                                 tz=tenant.timezone if tenant else "Europe/Moscow")
            await svc.publish_training(
                training.id,
                notify=(tenant.publish_notify_enabled if tenant else True))
