"""
Консоль владельца площадки в Telegram.

Веб-панель `/admin/platform` остаётся основным инструментом: там формы,
правки и всё, что требует ввода секретов. Здесь — быстрый просмотр с
телефона: кто подключён, сколько осталось аренды, как себя чувствует
платформа, и несколько безопасных действий.

Чего здесь СОЗНАТЕЛЬНО нет:

  * приёма и показа токенов ботов. История чата Telegram вечна, синхронно
    лежит на всех устройствах владельца и попадает в резервные копии
    самого мессенджера, а токен — это полный контроль над ботом клиента:
    чтение переписки, рассылка от его имени, смена вебхука. Поэтому новый
    клуб здесь только ЗАВОДИТСЯ, а токен вводится в вебе по ссылке.

  * логов открытым текстом. В них имена, id пользователей и названия
    клубов, то есть персональные данные. Файл шифруется тем же ключом,
    что и резервные копии (BACKUP_ENC_KEY) — тем же решением, по той же
    причине.

Доступ — строго у `PLATFORM_OWNER_TG_ID` и только в личном чате. Для всех
остальных команды не существуют: фильтр не совпадает, обработчик молчит и
не выдаёт даже факта, что такие команды есть.
"""
from __future__ import annotations

import datetime as dt
import html
import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import BufferedInputFile, Message

from app.core.config import settings
from app.db.engine import SessionLocal

logger = logging.getLogger("app")

router = Router(name="platform_console")

# Telegram режет сообщения на 4096 символах; оставляем запас на разметку.
_MAX_CLIENTS_IN_LIST = 40
_STARTED_AT = dt.datetime.now(dt.timezone.utc)


class OwnerOnly(BaseFilter):
    """Владелец площадки, и только в личном чате.

    В группе бот работает для участников клуба, и там панель оператора
    неуместна: её увидели бы посторонние."""

    async def __call__(self, message: Message) -> bool:
        owner = settings.platform_owner_tg_id
        if not owner:
            return False
        return (message.chat.type == "private"
                and message.from_user is not None
                and message.from_user.id == owner)


def _esc(text: str | None) -> str:
    return html.escape(str(text or ""))


def _days_left(paid_until: str | None) -> int | None:
    """Дней до конца аренды. None — срок не задан (без ограничения)."""
    raw = (paid_until or "").strip()
    if not raw:
        return None
    try:
        end = dt.date.fromisoformat(raw)
    except ValueError:
        return None
    return (end - dt.date.today()).days


def _rent_line(paid_until: str | None) -> str:
    days = _days_left(paid_until)
    if days is None:
        return "без ограничения"
    if days < 0:
        return f"ИСТЕКЛА {-days} дн. назад ({paid_until})"
    if days == 0:
        return f"истекает сегодня ({paid_until})"
    return f"{days} дн. ({paid_until})"


def _bots_line(tenant) -> str:
    """Какие площадки подключены. Только состояние, никогда не сам токен."""
    from app.core import bot_tokens

    parts = []
    for kind, label in (("tg", "TG"), ("vk", "VK")):
        if bot_tokens.has_token(tenant, kind):
            mode = getattr(tenant, f"{kind}_delivery_mode", "") or ""
            parts.append(f"{label}({mode})" if mode else label)
    return " · ".join(parts) if parts else "не подключены"


async def _tenants(session):
    from app.repositories.repo import GlobalRepository
    return await GlobalRepository(session).list_tenants()


# ---------- обзор клиентов ----------

@router.message(Command("clients"), OwnerOnly())
async def cmd_clients(message: Message) -> None:
    async with SessionLocal() as session:
        tenants = await _tenants(session)

    if not tenants:
        await message.answer("Клиентов пока нет. Завести: /newclub Название")
        return

    # первыми показываем тех, у кого аренда горит: ради этого список и нужен
    def _sort_key(t):
        days = _days_left(t.paid_until)
        return (0, days) if days is not None else (1, 0)

    shown = sorted(tenants, key=_sort_key)[:_MAX_CLIENTS_IN_LIST]
    lines = [f"<b>Клиенты платформы ({len(tenants)})</b>", ""]
    for t in shown:
        flags = []
        if getattr(t, "is_demo", False):
            flags.append("демо")
        if not t.is_active:
            flags.append("выключен")
        suffix = f" <i>({', '.join(flags)})</i>" if flags else ""
        days = _days_left(t.paid_until)
        mark = "🔴" if days is not None and days < 0 else (
            "🟡" if days is not None and days <= 3 else "🟢")
        lines.append(f"{mark} <b>#{t.id} {_esc(t.name)}</b>{suffix}")
        lines.append(f"    боты: {_esc(_bots_line(t))}")
        lines.append(f"    аренда: {_esc(_rent_line(t.paid_until))}")
    if len(tenants) > len(shown):
        lines.append("")
        lines.append(f"…и ещё {len(tenants) - len(shown)}. Полный список — "
                     "в веб-панели.")
    lines.append("")
    lines.append("Подробнее: /client &lt;id&gt;")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("client"), OwnerOnly())
async def cmd_client(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Как пользоваться: /client 3")
        return
    tenant_id = int(parts[1])

    from sqlalchemy import func, select

    from app.models.entities import Signup, Subscriber, Training
    from app.repositories.repo import GlobalRepository

    week_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    async with SessionLocal() as session:
        tenant = await GlobalRepository(session).get_tenant(tenant_id)
        if tenant is None:
            await message.answer(f"Клуба #{tenant_id} нет.")
            return

        async def _count(model, *where) -> int:
            stmt = select(func.count()).select_from(model).where(
                model.tenant_id == tenant_id, *where)
            return int((await session.execute(stmt)).scalar() or 0)

        trainings_week = await _count(Training, Training.created_at >= week_ago)
        signups_week = await _count(Signup, Signup.created_at >= week_ago)
        people = await _count(Subscriber)
        upcoming = await _count(
            Training, Training.start_at >= dt.datetime.now(dt.timezone.utc),
            Training.is_cancelled.is_(False))

    lines = [
        f"<b>#{tenant.id} {_esc(tenant.name)}</b>",
        f"состояние: {'активен' if tenant.is_active else 'выключен'}"
        + (" · демо" if getattr(tenant, 'is_demo', False) else ""),
        f"аренда: {_esc(_rent_line(tenant.paid_until))}",
        f"боты: {_esc(_bots_line(tenant))}",
        f"вертикаль: {_esc(getattr(tenant, 'vertical', '') or '—')}"
        f" · часовой пояс: {_esc(tenant.timezone)}",
        "",
        "<b>Нагрузка</b>",
        f"за 7 дней: занятий {trainings_week}, записей {signups_week}",
        f"предстоящих занятий: {upcoming}",
        f"участников всего: {people}",
    ]
    try:
        lines += ["", f"страница: {settings.public_url(f'/club/{tenant.id}')}"]
    except RuntimeError:
        pass          # PUBLIC_BASE_URL не задан — просто не показываем ссылку
    lines += ["", "Продлить аренду: /extend "
              f"{tenant.id} 30"]
    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------- нагрузка платформы ----------

@router.message(Command("load"), OwnerOnly())
async def cmd_load(message: Message) -> None:
    from app.main import _rss_mb, error_counters
    from app.repositories.repo import GlobalRepository

    async with SessionLocal() as session:
        g = GlobalRepository(session)
        tenants = await g.list_tenants()
        health = await g.outbox_health()

    up_min = int((dt.datetime.now(dt.timezone.utc)
                  - _STARTED_AT).total_seconds() // 60)
    rss = _rss_mb()
    active = sum(1 for t in tenants if t.is_active)
    expired = sum(1 for t in tenants
                  if (d := _days_left(t.paid_until)) is not None and d < 0)

    lines = [
        "<b>Нагрузка площадки</b>",
        f"память: {rss if rss is not None else '—'} МБ"
        f" · процесс живёт {up_min // 60} ч {up_min % 60} мин",
        f"клубов: {len(tenants)} (активных {active}, с истёкшей арендой "
        f"{expired})",
        "",
        "<b>Очередь уведомлений</b>",
        f"самое старое ждёт: {health['pending_age_min']} мин",
        f"недоставлено (сбои): {health['dead']}",
        f"снято как недоставляемое: {health['dead_no_channel']}",
    ]
    errors = error_counters()
    if errors:
        lines += ["", "<b>Ошибки ответов с момента старта</b>"]
        for key, count in list(errors.items())[:5]:
            lines.append(f"{_esc(key)}: {count}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------- логи ----------

@router.message(Command("logs"), OwnerOnly())
async def cmd_logs(message: Message) -> None:
    """Отдаёт журнал ошибок ЗАШИФРОВАННЫМ файлом.

    В журнале имена, id пользователей и названия клубов. Открытым текстом
    в чат такое не отправляют — это то же решение и та же причина, что у
    резервных копий."""
    from pathlib import Path

    from app.services.backup import encrypt_backup, encryption_enabled

    if not encryption_enabled():
        await message.answer(
            "BACKUP_ENC_KEY не задан. Журнал содержит персональные данные и "
            "без шифрования не отправляется — задайте ключ в переменных "
            "окружения.")
        return

    path = Path(settings.log_dir) / "errors.log"
    if not path.exists() or path.stat().st_size == 0:
        await message.answer("Журнал ошибок пуст — с момента запуска ошибок "
                             "уровня ERROR не было.")
        return

    raw = path.read_bytes()
    tail = raw[-200_000:]          # хвост: целиком журнал в чат не нужен
    lines_count = tail.count(b"\n")
    try:
        blob = encrypt_backup(tail)
    except Exception as e:          # noqa: BLE001
        logger.error("Журнал не зашифрован: %s", type(e).__name__)
        await message.answer("Не удалось зашифровать журнал — не отправляю.")
        return

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    await message.answer_document(
        BufferedInputFile(blob, f"errors_{stamp}.log.enc"),
        caption=(f"Журнал ошибок, последние ~{lines_count} строк.\n"
                 "Зашифрован ключом резервных копий: расшифровать — "
                 "scripts/restore_backup.py"))


# ---------- действия ----------

@router.message(Command("newclub"), OwnerOnly())
async def cmd_newclub(message: Message) -> None:
    """Заводит клуб. Токен бота здесь НЕ принимается — только в вебе."""
    name = (message.text or "").partition(" ")[2].strip()
    if len(name) < 2:
        await message.answer("Как пользоваться: /newclub Название клуба")
        return

    from app.models.entities import Tenant

    async with SessionLocal() as session:
        tenant = Tenant(name=name[:200])
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    lines = [f"Клуб <b>#{tenant_id} {_esc(name)}</b> заведён.", ""]
    try:
        lines.append("Токен бота введите в веб-панели: "
                     + settings.public_url(f"/admin/platform/{tenant_id}/edit"))
    except RuntimeError:
        lines.append("Токен бота введите в веб-панели, раздел клуба.")
    lines += ["", "<i>Токен в переписку не отправляйте: история чата вечна "
              "и лежит на всех ваших устройствах, а токен — это полный "
              "доступ к боту клиента.</i>"]
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("extend"), OwnerOnly())
async def cmd_extend(message: Message) -> None:
    """Продлевает аренду: /extend <id> <дней>."""
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Как пользоваться: /extend 3 30")
        return
    tenant_id, days = int(parts[1]), int(parts[2])
    if not 1 <= days <= 366:
        await message.answer("Продлевать можно на 1–366 дней.")
        return

    from app.repositories.repo import GlobalRepository

    async with SessionLocal() as session:
        tenant = await GlobalRepository(session).get_tenant(tenant_id)
        if tenant is None:
            await message.answer(f"Клуба #{tenant_id} нет.")
            return
        # продлеваем от текущей даты окончания, но не раньше сегодняшнего дня:
        # иначе оплата задним числом «сгорала» бы целиком
        today = dt.date.today()
        current = today
        if (tenant.paid_until or "").strip():
            try:
                current = max(dt.date.fromisoformat(tenant.paid_until), today)
            except ValueError:
                current = today
        tenant.paid_until = (current + dt.timedelta(days=days)).isoformat()
        new_value = tenant.paid_until
        await session.commit()

    await message.answer(
        f"Клуб #{tenant_id}: аренда продлена на {days} дн., "
        f"теперь до {new_value}.")


@router.message(Command("console"), OwnerOnly())
@router.message(F.text.lower() == "консоль", OwnerOnly())
async def cmd_console(message: Message) -> None:
    await message.answer(
        "<b>Консоль площадки</b>\n\n"
        "/clients — все клиенты, аренда, подключённые боты\n"
        "/client &lt;id&gt; — карточка клуба и его нагрузка\n"
        "/load — память, очередь уведомлений, ошибки\n"
        "/logs — журнал ошибок (зашифрованным файлом)\n"
        "/newclub &lt;название&gt; — завести клуб\n"
        "/extend &lt;id&gt; &lt;дней&gt; — продлить аренду\n\n"
        "<i>Правки и токены — в веб-панели: в переписку секреты не "
        "отправляем.</i>",
        parse_mode="HTML")
