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


async def supervise(name: str, coro_factory: Callable[[], Awaitable[None]],
                    base_backoff: float = 5.0) -> None:
    """
    Самовосстановление фоновой задачи: run_polling ботов (TG/VK) рассчитан
    на бесконечную работу, поэтому если он вообще завершился с исключением —
    это ненормально (сеть, сбой API и т.п.). Перезапускаем с нарастающей
    паузой и алертим владельца площадки после нескольких падений подряд
    (тем же механизмом, что и ошибки планировщика) — иначе бот молча
    замолкает для всех клиентов до ручного рестарта на Railway.

    Если задача завершилась БЕЗ исключения (например, токен бота не
    настроен и функция сразу вернулась) — это штатное поведение,
    перезапускать не нужно.
    """
    backoff = base_backoff
    fails = 0
    while True:
        try:
            await coro_factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fails += 1
            logger.exception("Фоновая задача '%s' упала (%d раз подряд)",
                             name, fails)
            if fails >= 3:
                await _alert_admins(name, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# после стольких неудачных попыток сообщение считается недоставляемым и
# снимается с очереди (не ретраим вечно — например, бот заблокирован юзером)
MAX_OUTBOX_ATTEMPTS = 5


async def _deliver_once() -> None:
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        for platform, sender in _senders.items():
            pending = await g.fetch_pending_outbox(platform, limit=25)
            for item in pending:
                try:
                    try:
                        await sender(item.user_id, item.text,
                                     tenant_id=getattr(item, "tenant_id", None))
                    except TypeError:
                        await sender(item.user_id, item.text)
                    await g.mark_outbox_sent(item.id)
                except Exception:
                    attempts = (item.attempts or 0) + 1
                    if attempts >= MAX_OUTBOX_ATTEMPTS:
                        logger.warning(
                            "Доставка %s user=%s не удалась %d раз подряд — "
                            "отказываемся, сообщение снято с очереди",
                            platform, item.user_id, attempts)
                        await g.mark_outbox_sent(item.id)
                    else:
                        logger.info(
                            "Доставка %s user=%s не удалась (попытка %d/%d), "
                            "повторим на следующем проходе",
                            platform, item.user_id, attempts, MAX_OUTBOX_ATTEMPTS)
                        await g.record_outbox_failure(item.id, attempts)
            await session.commit()


async def scheduler_loop() -> None:
    _last_backup_day = [None]
    _last_offsite_backup_day = [None]
    _last_maint_day = [None]
    while True:
        try:
            await _run_scheduler()
        except Exception as e:
            logger.exception("Ошибка планировщика")
            await _alert_admins("планировщик напоминаний", e)
        try:
            await _process_schedules()
        except Exception as e:
            logger.exception("Ошибка обработки расписаний")
            await _alert_admins("расписание", e)
        try:
            _auto_backup(_last_backup_day)
        except Exception:
            logger.exception("Ошибка автобэкапа")
        try:
            await _offsite_backup(_last_offsite_backup_day)
        except Exception as e:
            logger.exception("Ошибка внешнего бэкапа")
            await _alert_admins("внешний бэкап", e)
        try:
            await _daily_maintenance(_last_maint_day)
        except Exception:
            logger.exception("Ошибка ежедневного обслуживания")
        await asyncio.sleep(60)


_alerted: set = set()


async def _alert_admins(where: str, err: Exception) -> None:
    """Пишет владельцам клубов в личку о сбое фоновой задачи.
    Каждый тип ошибки шлётся раз в час, чтобы не спамить."""
    key = f"{where}:{type(err).__name__}"
    now = dt.datetime.now(dt.timezone.utc)
    last = _alerted_time.get(key)
    if last and (now - last).total_seconds() < 3600:
        return
    _alerted_time[key] = now
    text = (f"⚠️ Сбой в фоновой задаче ({where}): "
            f"{type(err).__name__}: {str(err)[:200]}")
    try:
        async with SessionLocal() as session:
            from sqlalchemy import select
            from app.models.entities import Tenant
            tenants = list((await session.execute(select(Tenant))).scalars())
        tg_sender = _senders.get("tg")
        for t in tenants:
            if t.admin_tg_id and tg_sender:
                try:
                    await tg_sender(t.admin_tg_id, text, tenant_id=t.id)
                except Exception:
                    pass
    except Exception:
        logger.warning("Не удалось разослать алерт админам")


_alerted_time: dict = {}


def _auto_backup(last_day: list) -> None:
    """Раз в сутки делает копию SQLite-базы в /data/backups (хранит 7 последних)."""
    import os
    import sqlite3
    from app.core.config import settings
    if not settings.is_sqlite:
        return
    today = dt.date.today().isoformat()
    if last_day[0] == today:
        return
    url = settings.database_url
    tail = url.split("///")[-1]
    path = ("/" + tail if url.count("/") >= 4 and not tail.startswith("/")
            else tail)
    if not os.path.exists(path):
        return
    backup_dir = os.path.join(os.path.dirname(path) or ".", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, f"backup_{today}.db")
    src = sqlite3.connect(path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    # оставляем 7 последних
    files = sorted(f for f in os.listdir(backup_dir)
                   if f.startswith("backup_") and f.endswith(".db"))
    for old in files[:-7]:
        try:
            os.remove(os.path.join(backup_dir, old))
        except OSError:
            pass
    last_day[0] = today
    logger.info("Автобэкап базы сохранён: %s", dest)


async def _offsite_backup(last_day: list) -> None:
    """Раз в сутки отправляет дамп базы (Postgres или SQLite) владельцу
    площадки в Telegram — в отличие от _auto_backup (копия на том же
    Railway-диске), эта копия хранится ВНЕ платформы и переживает полное
    падение Railway. См. app/services/backup.py и DISASTER_RECOVERY.md."""
    today = dt.date.today().isoformat()
    if last_day[0] == today:
        return
    last_day[0] = today
    from app.services import backup
    result = await backup.send_backup_to_owner()
    logger.info("Внешний бэкап: %s", result)


async def _process_schedules() -> None:
    """Создаёт тренировки по регулярным расписаниям.
    Для каждого активного шаблона: если ближайшее занятие наступает в течение
    schedule.days_ahead дней и на эту дату ещё не создавалось — создаёт и
    оповещает подписчиков клуба."""
    from sqlalchemy import select
    from app.models.entities import Schedule
    async with SessionLocal() as session:
        schedules = list((await session.execute(
            select(Schedule).where(Schedule.active.is_(True)))).scalars().all())
        if not schedules:
            return
        g = GlobalRepository(session)
        for sch in schedules:
            tenant = await g.get_tenant(sch.tenant_id)
            if tenant is None:
                continue
            from app.core.config import tenant_suspended
            if tenant_suspended(tenant):
                continue
            svc = BookingService(session, sch.tenant_id, tz=tenant.timezone)
            # ближайшая дата занятия по дню недели (в таймзоне клуба)
            today = dt.datetime.now(svc.tz).date()
            delta = (sch.weekday - today.weekday()) % 7
            occ_date = today + dt.timedelta(days=delta)
            # если сегодня этот день, но время уже прошло — берём следующую неделю
            occ_start = svc.parse_local(
                f"{occ_date.day:02d}.{occ_date.month:02d}.{occ_date.year} {sch.time_str}")
            if occ_start <= dt.datetime.now(dt.timezone.utc):
                occ_date = occ_date + dt.timedelta(days=7)
                occ_start = svc.parse_local(
                    f"{occ_date.day:02d}.{occ_date.month:02d}.{occ_date.year} {sch.time_str}")
            # рано создавать?
            if (occ_date - today).days > sch.days_ahead:
                continue
            # уже создавали на эту дату?
            if sch.last_date == occ_date.isoformat():
                continue
            training = await svc.create_training(
                title=sch.title, start_at=occ_start, location=sch.location,
                max_participants=sch.max_participants,
                duration_min=sch.duration_min, state="published",
                publish_at=None, platform="api", user_id=0)
            if sch.price_minor:
                training.price_minor = sch.price_minor
            sch.last_date = occ_date.isoformat()
            # оповещаем подписчиков клуба
            when = svc.format_local(training.start_at)
            note = (f"🏸 Открыта запись на «{training.title}»\n📅 {when}"
                    + (f"\n📍 {training.location}" if training.location else ""))
            for sub in await svc.repo.get_subscribers():
                await svc.repo.enqueue(sub.platform, sub.user_id, note)
            await session.commit()
            # публикуем карточку в Telegram-группу и анонс на стену ВК
            try:
                from app.bots import telegram as _tg
                await _tg._publish_to_group(sch.tenant_id, training.id)
            except Exception as e:
                logger.warning("Расписание: публикация в TG не удалась: %s", e)
            try:
                from app.bots import vk as _vk
                await _vk.publish_to_wall(sch.tenant_id, training.id)
            except Exception as e:
                logger.warning("Расписание: анонс на стену не удался: %s", e)
            logger.info("Расписание: создана «%s» на %s", training.title, when)


async def _run_scheduler() -> None:
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
            from app.core.config import tenant_suspended
            if tenant_suspended(tenant):
                continue
            svc = BookingService(session, training.tenant_id, tz=tenant.timezone)
            # SQLite возвращает naive-даты — считаем их UTC (так и хранится)
            start_at = (training.start_at if training.start_at.tzinfo
                        else training.start_at.replace(tzinfo=dt.timezone.utc))
            minutes_left = (start_at - now).total_seconds() / 60
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
                    # карточка в TG-группе обновляется один раз после всех
                    # снятий (не в цикле — не спамим edit_message на гостя)
                    try:
                        from app.bots import telegram as _tg
                        await _tg._refresh_group_card(training.tenant_id, training.id)
                    except Exception as e:
                        logger.debug("Не удалось обновить карточку в группе: %s", e)
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


async def _daily_maintenance(last_day: list) -> None:
    """Раз в сутки: чистка очереди сообщений, уведомление клиентов об
    истекающей/истёкшей оплате их клуба (в их же боте) и сводка владельцу
    платформы."""
    today = dt.date.today().isoformat()
    if last_day[0] == today:
        return
    last_day[0] = today
    from sqlalchemy import delete, select
    from app.core.config import settings
    from app.models.entities import Outbox, Tenant
    from app.repositories.repo import TenantRepository

    async with SessionLocal() as session:
        # отправленные сообщения старше 30 дней и "web"-хвосты больше не нужны
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        await session.execute(delete(Outbox).where(
            (Outbox.platform == "web")
            | ((Outbox.sent.is_(True)) & (Outbox.created_at < cutoff))))
        await session.commit()

        # SaaS: клубы с истекающей (≤3 дня) или уже истёкшей оплатой
        tenants = list((await session.execute(select(Tenant))).scalars())
        soon = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        today_s = today
        expiring = [t for t in tenants if (t.paid_until or "").strip()
                    and t.paid_until <= soon]

        # уведомление клиенту (тренеру/владельцу клуба) в его собственном
        # боте. Маркер last_billing_notice — чтобы не слать одно и то же
        # каждый день: одно сообщение на переход в "скоро истекает" и одно
        # на сам факт истечения. При продлении оплаты (новый paid_until)
        # маркер естественным образом устаревает и уведомления возобновятся.
        for t in expiring:
            stage = "expired" if t.paid_until < today_s else "soon"
            marker = f"{t.paid_until}:{stage}"
            if t.last_billing_notice == marker or not (t.admin_tg_id or t.admin_vk_id):
                continue
            contact = (f" Для продления свяжитесь: {settings.platform_support_contact}."
                      if settings.platform_support_contact else
                      " Для продления свяжитесь с администрацией сервиса.")
            if stage == "expired":
                text = (f"🚫 Подписка клуба «{t.name}» истекла {t.paid_until}. "
                        f"Бот и страница записи временно приостановлены — "
                        f"участники не могут записываться.{contact}")
            else:
                text = (f"⚠️ Подписка клуба «{t.name}» истекает {t.paid_until}. "
                        f"Чтобы бот не останавливался, продлите заранее.{contact}")
            repo = TenantRepository(session, t.id)
            if t.admin_tg_id:
                await repo.enqueue("tg", t.admin_tg_id, text)
            if t.admin_vk_id:
                await repo.enqueue("vk", t.admin_vk_id, text)
            t.last_billing_notice = marker
        await session.commit()

    # сводка владельцу платформы — вне сессии, только чтение уже
    # загруженных скалярных полей (name/id/paid_until) и отправка сообщения
    if not expiring:
        return
    owner_tg_id = settings.platform_owner_tg_id
    tg_sender = _senders.get("tg")
    if owner_tg_id and tg_sender:
        lines = ["💳 Оплата клубов (SaaS):"]
        for t in expiring:
            state = "ИСТЕКЛА" if t.paid_until < today_s else f"до {t.paid_until}"
            lines.append(f"  • {t.name} (id={t.id}): {state}")
        try:
            await tg_sender(owner_tg_id, "\n".join(lines))
        except Exception:
            pass
