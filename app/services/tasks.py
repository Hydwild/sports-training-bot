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
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from app.db.engine import SessionLocal
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService

logger = logging.getLogger("tasks")

# platform -> async функция (user_id, text) -> None
Sender = Callable[[int, str], Awaitable[None]]
_senders: dict[str, Sender] = {}
_outbox_wakeup = asyncio.Event()


def register_sender(platform: str, sender: Sender) -> None:
    _senders[platform] = sender


def unregister_sender(platform: str, sender: Sender | None = None) -> None:
    if sender is None or _senders.get(platform) is sender:
        _senders.pop(platform, None)


def notify_outbox_committed() -> None:
    """Будит доставщик только после commit транзакции с новым Outbox."""
    _outbox_wakeup.set()


@dataclass(frozen=True)
class DeliveryResult:
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    revived: int = 0
    buried: int = 0


async def deliver_outbox_loop() -> None:
    from app.core.config import settings

    idle = settings.outbox_idle_min_seconds
    last_requeue = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    while True:
        try:
            _outbox_wakeup.clear()
            now = dt.datetime.now(dt.timezone.utc)
            requeue_stale = (now - last_requeue).total_seconds() >= 60
            result = await _deliver_once(requeue_stale=requeue_stale)
            if requeue_stale:
                last_requeue = now
            if result.claimed:
                # После работы сразу проверяем остаток; в idle уходим только
                # после подтверждения, что готовых сообщений больше нет.
                idle = settings.outbox_idle_min_seconds
                continue
            delay = idle
            idle = min(settings.outbox_idle_max_seconds, max(
                settings.outbox_idle_min_seconds, idle * 2,
            ))
            try:
                await asyncio.wait_for(_outbox_wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
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


# через сколько минут захват считается протухшим (процесс убили посреди
# отправки) и сообщение возвращается в очередь
STALE_CLAIM_MINUTES = 10

# пороги для суточной сводки владельцу: сколько недоставленных уже похоже
# на общий сбой, и сколько минут ожидания означает вставшую очередь
DEAD_LETTER_ALERT = 20
PENDING_AGE_ALERT_MIN = 30


# Гарантия доставки — «хотя бы один раз», не «ровно один раз».
#
# Сообщение помечается доставленным ПОСЛЕ ответа Telegram/VK. Если процесс
# умрёт между ответом API и записью в базу, сообщение вернётся в очередь
# (см. requeue_stale_outbox) и уйдёт повторно — человек получит дубль.
# Обещать exactly-once здесь нельзя: ни Telegram, ни VK не дают ключа
# идемпотентности для sendMessage, поэтому отличить «уже отправлено» от
# «не отправлено» на их стороне нечем. Выбор осознанный: лучше редкий
# дубль напоминания, чем молча потерянное уведомление.


async def _deliver_once(*, requeue_stale: bool = True) -> DeliveryResult:
    claimed_count = sent_count = failed_count = buried_count = revived_count = 0
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        # сначала подбираем то, что зависло в processing после перезапуска
        revived = (await g.requeue_stale_outbox(STALE_CLAIM_MINUTES)
                   if requeue_stale else 0)
        if revived:
            revived_count += revived
            logger.warning("Вернули в очередь %d зависших сообщений "
                           "(процесс прервали посреди отправки)", revived)
            await session.commit()
        # Сообщения платформ без канала доставки (например web — у веб-клиента
        # нет мессенджера) никогда не будут захвачены циклом ниже: он ходит
        # только по зарегистрированным senders. Такие строки висели в pending
        # вечно и держали алерт «очередь не разгребается» включённым, скрывая
        # за собой настоящие сбои. Хороним их честно, с причиной.
        # причина берётся из общей константы: по ней же такие сообщения
        # отделяются от настоящих сбоев доставки в суточной сводке
        buried = await g.dead_letter_undeliverable(list(_senders))
        if buried:
            buried_count += buried
            logger.warning("Похоронили %d сообщений платформ без канала "
                           "доставки (подключённые: %s)", buried,
                           ", ".join(sorted(_senders)) or "нет")
            await session.commit()
        for platform, sender in _senders.items():
            # claim_pending_outbox сразу помечает захваченные записи sent=True
            # (UPDATE ... WHERE sent=False ... RETURNING) — коммитим это до
            # начала отправки, чтобы другой экземпляр приложения (если
            # когда-нибудь будет работать несколько одновременно) не увидел
            # эти же записи как ещё не отправленные и не продублировал их
            pending = await g.claim_pending_outbox(platform, limit=25)
            claimed_count += len(pending)
            await session.commit()
            for item in pending:
                try:
                    try:
                        await sender(item.user_id, item.text,
                                     tenant_id=getattr(item, "tenant_id", None))
                    except TypeError:
                        await sender(item.user_id, item.text)
                    await g.mark_outbox_sent(item.id)
                    sent_count += 1
                except Exception as e:
                    failed_count += 1
                    attempts = (item.attempts or 0) + 1
                    reason = f"{type(e).__name__}: {e}"
                    if attempts >= MAX_OUTBOX_ATTEMPTS:
                        logger.warning(
                            "Доставка %s user=%s не удалась %d раз подряд — "
                            "сообщение помечено недоставленным: %s",
                            platform, item.user_id, attempts, reason)
                        await g.mark_outbox_dead(item.id, reason)
                    else:
                        logger.info(
                            "Доставка %s user=%s не удалась (попытка %d/%d), "
                            "повторим на следующем проходе: %s",
                            platform, item.user_id, attempts,
                            MAX_OUTBOX_ATTEMPTS, reason)
                        await g.record_outbox_failure(item.id, attempts, reason)
            await session.commit()
    return DeliveryResult(
        claimed=claimed_count,
        sent=sent_count,
        failed=failed_count,
        revived=revived_count,
        buried=buried_count,
    )


async def scheduler_loop() -> None:
    _last_backup_day = [None]
    _last_offsite_backup_day = [None]
    _last_maint_day = [None]
    _last_demo_reset_day = [None]
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
        try:
            await _demo_reset_daily(_last_demo_reset_day)
        except Exception:
            logger.exception("Ошибка сброса демо-клубов")
        try:
            await _admin_daily_digest()
        except Exception:
            logger.exception("Ошибка утреннего дайджеста")
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
    from app.services import backup
    result = await backup.send_backup_to_owner()
    if not result.ok:
        # копии за сегодня НЕТ: день не помечаем — планировщик повторит
        # попытку на следующем проходе. Раньше день закрывался при любом
        # исходе, и «бэкап не ушёл» молча превращался в «бэкап за сегодня
        # сделан». Владельца оповещаем, но не чаще раза в час (_alert_admins).
        logger.error("Внешний бэкап НЕ выполнен: %s", result.message)
        await _alert_admins("внешний бэкап", RuntimeError(result.message))
        return
    last_day[0] = today
    logger.info("Внешний бэкап: %s", result.message)


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
        # доставленные сообщения старше 30 дней и "web"-хвосты больше не
        # нужны. Недоставленные (status='dead') НЕ трогаем: это диагностика,
        # по ней видно, что кому-то перестали приходить уведомления
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        await session.execute(delete(Outbox).where(
            (Outbox.platform == "web")
            | (Outbox.status.in_(("sent", "discarded"))
               & (Outbox.handled_at.is_not(None))
               & (Outbox.handled_at < cutoff))
            # у доставленных до появления handled_at её нет — падаем на дату
            # создания, иначе они останутся навсегда
            | ((Outbox.status == "sent") & (Outbox.handled_at.is_(None))
               & (Outbox.created_at < cutoff))))
        await session.commit()

        # отработавшие окна лимита
        from app.api.rate_limit import purge_old_buckets
        await purge_old_buckets(session)
        # истёкшие короткие сессии управления по всем клубам
        from sqlalchemy import delete as _delete
        from app.models.entities import ManageSession
        await session.execute(_delete(ManageSession).where(
            ManageSession.expires_at < dt.datetime.now(dt.timezone.utc)))
        await session.commit()

        from app.repositories.repo import GlobalRepository
        health = await GlobalRepository(session).outbox_health()
        dead_count = health["dead"]
        no_channel_count = health["dead_no_channel"]
        pending_age = health["pending_age_min"]

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
    owner_tg_id = settings.platform_owner_tg_id
    tg_sender = _senders.get("tg")
    if not (owner_tg_id and tg_sender):
        return

    lines: list[str] = []
    if expiring:
        lines.append("💳 Оплата клубов (SaaS):")
        for t in expiring:
            state = "ИСТЕКЛА" if t.paid_until < today_s else f"до {t.paid_until}"
            lines.append(f"  • {t.name} (id={t.id}): {state}")
    # молчаливо терять уведомления нельзя: если что-то так и не дошло,
    # владелец должен об этом узнать
    if dead_count:
        if lines:
            lines.append("")
        lines.append(f"📮 Недоставленных уведомлений: {dead_count}. "
                     "Обычно это заблокированный бот или удалённый чат. "
                     "Разобрать: /admin/platform/outbox")
        if dead_count >= DEAD_LETTER_ALERT:
            lines.append(f"⚠️ Это больше порога ({DEAD_LETTER_ALERT}) — "
                         "похоже на общий сбой доставки, а не на "
                         "единичные блокировки.")
    # Сообщения, которые слать было НЕКУДА (площадка не подключена), к
    # сбоям доставки не относятся и порог не двигают — иначе разовая уборка
    # старой очереди заставила бы алерт кричать каждый день, и владелец
    # перестал бы его читать. Показываем отдельной строкой, без тревоги.
    if no_channel_count:
        if lines:
            lines.append("")
        lines.append(f"🗄 Снято с очереди как недоставляемое: "
                     f"{no_channel_count}. Это сообщения площадок, которые "
                     "не подключены (например, VK без токена) — отправлять "
                     "их было некуда. Сбоем доставки не считается.")
    if pending_age >= PENDING_AGE_ALERT_MIN:
        # очередь не разгребается: доставка встала, а не «иногда не доходит»
        if lines:
            lines.append("")
        lines.append(f"⏳ Самое старое сообщение ждёт отправки "
                     f"{pending_age} мин — очередь не разгребается.")
    if not lines:
        return
    try:
        await tg_sender(owner_tg_id, "\n".join(lines))
    except Exception:
        pass


# демо-тренировки, создаваемые заново при каждом ночном сбросе (дни/время —
# относительно момента сброса, чтобы демо всегда выглядело "живым")
_DEMO_SEED = [
    {"title": "Вечерняя игра", "days": 1, "hour": 19, "duration_min": 90,
     "max_participants": 8, "location": "Зал №1", "coach": 0},
    {"title": "Утренняя тренировка", "days": 2, "hour": 9, "duration_min": 60,
     "max_participants": 6, "location": "Зал №2", "coach": 1},
    {"title": "Турнир выходного дня", "days": 5, "hour": 12, "duration_min": 180,
     "max_participants": 16, "location": "Главный корт", "coach": 0},
]

# тренеры демо-клуба: витрина должна выглядеть прилично для показа клиентам
_DEMO_MASTERS = [
    {"name": "Алексей Морозов", "specialty": "Старший тренер",
     "bio": "Мастер спорта, опыт 12 лет. Групповые и персональные занятия."},
    {"name": "Ирина Соколова", "specialty": "Тренер по ОФП",
     "bio": "Опыт 6 лет, специализация — начинающие и юниоры."},
]

# витрина демо-клуба (обложка не задаётся — только текст, чтобы не зависеть
# от внешней картинки)
_DEMO_PROFILE = {
    "about": ("Демо-клуб платформы: здесь можно посмотреть, как выглядит "
              "страница записи. Данные обновляются каждую ночь."),
    "address": "г. Москва, ул. Спортивная, 1",
    "contact_phone": "+7 900 000-00-00",
}


# час (в таймзоне клуба), начиная с которого отправляется утренний дайджест
DIGEST_HOUR = 8


async def _admin_daily_digest() -> None:
    """Утренний дайджест админам клубов: сколько человек записано на
    сегодняшние слоты/тренировки. Отправляется раз в сутки после
    DIGEST_HOUR по местному времени клуба; маркер last_digest_date хранится
    в базе — рестарт сервиса не приводит к повторной отправке. Демо-клубы
    пропускаются (данные фиктивные). Если на сегодня ничего нет — день
    помечается без отправки (не спамим пустыми сводками)."""
    from app.models.entities import Tenant
    from app.repositories.repo import TenantRepository
    from sqlalchemy import select

    async with SessionLocal() as session:
        tenants = list((await session.execute(
            select(Tenant).where(Tenant.is_active.is_(True)))).scalars())
        for t in tenants:
            if t.is_demo or not (t.admin_tg_id or t.admin_vk_id):
                continue
            svc = BookingService(session, t.id, tz=t.timezone)
            local_now = dt.datetime.now(svc.tz)
            today = local_now.date().isoformat()
            if t.last_digest_date == today or local_now.hour < DIGEST_HOUR:
                continue
            repo = TenantRepository(session, t.id)
            todays = []
            for tr in await repo.list_upcoming():
                start = (tr.start_at if tr.start_at.tzinfo
                         else tr.start_at.replace(tzinfo=dt.timezone.utc))
                if start.astimezone(svc.tz).date().isoformat() == today:
                    todays.append((start.astimezone(svc.tz), tr))
            t.last_digest_date = today
            if not todays:
                await session.commit()
                continue
            masters = await repo.masters_map()
            lines = [f"📋 Записи на сегодня, {local_now.strftime('%d.%m')}:"]
            total = 0
            for local_start, tr in sorted(todays, key=lambda x: x[0]):
                active = await repo.get_signups(tr.id, "active")
                queue = await repo.get_signups(tr.id, "queue")
                total += len(active)
                m = masters.get(tr.master_id) if tr.master_id else None
                master = f", {m.name}" if m else ""
                q = f" (+{len(queue)} в очереди)" if queue else ""
                lines.append(
                    f"• {local_start.strftime('%H:%M')} «{tr.title}»{master} — "
                    f"{len(active)}/{tr.max_participants}{q}")
            lines.append(f"Всего записано: {total}")
            text = "\n".join(lines)
            if t.admin_tg_id:
                await repo.enqueue("tg", t.admin_tg_id, text)
            if t.admin_vk_id:
                await repo.enqueue("vk", t.admin_vk_id, text)
            await session.commit()


async def _demo_reset_daily(last_day: list) -> None:
    """Раз в сутки полностью пересобирает демо-клубы (Tenant.is_demo=True):
    любой посетитель демо-бота может создавать/удалять тренировки и
    становиться "тренером" — без сброса демо быстро превращается в свалку.
    Тренировки/записи/роли участников/очередь уведомлений удаляются,
    создаётся свежий набор примерных тренировок. Реальные (не демо) клубы
    не затрагиваются."""
    today = dt.date.today().isoformat()
    if last_day[0] == today:
        return
    from sqlalchemy import delete, select
    from app.models.entities import Membership, Outbox, Schedule, Tenant, Training

    async with SessionLocal() as session:
        demo_tenants = list((await session.execute(
            select(Tenant).where(Tenant.is_demo.is_(True)))).scalars())
        for t in demo_tenants:
            # Training удаляется каскадом вместе со своими Signup/Payment
            # (ondelete=CASCADE в моделях) — отдельно чистим только то, что
            # с Training FK не связано.
            for model in (Training, Membership, Outbox, Schedule):
                await session.execute(delete(model).where(model.tenant_id == t.id))
            svc = BookingService(session, t.id, tz=t.timezone)
            now = dt.datetime.now(dt.timezone.utc)
            # витрина и тренеры — чтобы демо было презентабельным без
            # ручной настройки (Master удаляется каскадом вместе со слотами
            # только при удалении клуба, здесь чистим сами)
            from sqlalchemy import delete as _delete
            from app.models.entities import Master
            await session.execute(
                _delete(Master).where(Master.tenant_id == t.id))
            masters = []
            for m in _DEMO_MASTERS:
                masters.append(await svc.repo.add_master(
                    name=m["name"], specialty=m["specialty"], bio=m["bio"]))
            if not (t.about or "").strip():
                t.about = _DEMO_PROFILE["about"]
            if not (t.address or "").strip():
                t.address = _DEMO_PROFILE["address"]
            if not (t.contact_phone or "").strip():
                t.contact_phone = _DEMO_PROFILE["contact_phone"]
            for item in _DEMO_SEED:
                start_at = (now + dt.timedelta(days=item["days"])).replace(
                    hour=item["hour"], minute=0, second=0, microsecond=0)
                coach_idx = item.get("coach")
                master_id = (masters[coach_idx].id
                             if coach_idx is not None and coach_idx < len(masters)
                             else None)
                await svc.repo.add_training(
                    title=item["title"], start_at=start_at, location=item["location"],
                    max_participants=item["max_participants"],
                    duration_min=item["duration_min"], state="published",
                    publish_at=None, created_by_platform="system", created_by_id=0,
                    master_id=master_id)
            await session.commit()
        logger.info("Демо-клубы пересобраны: %d", len(demo_tenants))
    last_day[0] = today
