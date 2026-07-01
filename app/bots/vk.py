"""
VK-бот (vkbottle), мультитенантный. Импорт vkbottle ленивый.

Работа в личных сообщениях сообщества:
  • Long Poll — бот сам опрашивает VK (run_polling).
  • Текстовые команды: список, профиль, стоп.
  • Inline-кнопки (Callback) под карточкой: записаться / отмена.
    Нажатия приходят как message_event и обрабатываются _on_callback.

Callback API (webhook) тоже поддержан через feed_callback_event.
"""
from __future__ import annotations

import json
import logging

from app.bots.user_info import fetch_vk_profile
from app.bots import views
from app.bots.views import profile_card
from app.core.config import settings
from app.db.engine import SessionLocal
from app.repositories.repo import GlobalRepository
from app.services import tasks
from app.services.booking import BookingService

logger = logging.getLogger("vk")
PLATFORM = "vk"

_bot = None   # type: ignore
_enabled = False
_group_id = None   # id сообщества, к которому привязан токен


async def _send(user_id: int, text: str, keyboard: str | None = None) -> None:
    if _bot:
        await _bot.api.messages.send(
            user_id=user_id, message=text, random_id=0, keyboard=keyboard)


def _kb(tid: int, is_full: bool = False) -> str:
    """Inline-клавиатура под карточкой тренировки (записаться / отмена)."""
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    signup = "⏳ Встать в очередь" if is_full else "✅ Записаться"
    kb = Keyboard(inline=True)
    kb.add(Callback(signup, payload={"a": "su", "tid": tid}),
           color=KeyboardButtonColor.POSITIVE)
    kb.add(Callback("❌ Отменить", payload={"a": "cx", "tid": tid}),
           color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def _menu_kb() -> str:
    """Постоянное меню снизу: список / профиль."""
    from vkbottle import Keyboard, Text
    kb = Keyboard(inline=False, one_time=False)
    kb.add(Text("🏸 Тренировки", payload={"a": "list"}))
    kb.add(Text("👤 Профиль", payload={"a": "profile"}))
    return kb.get_json()


async def _upsert_vk_user(svc: BookingService, user_id: int) -> str:
    if not _bot:
        return f"vk{user_id}"
    try:
        profile = await fetch_vk_profile(_bot.api, user_id)
        await svc.repo.upsert_subscriber(
            PLATFORM, user_id, profile.name,
            username=profile.username, photo_url=profile.photo_url)
        return profile.name
    except Exception as e:
        logger.warning("Не удалось получить профиль VK %s: %s", user_id, e)
        return f"vk{user_id}"


async def _resolve_tenant(session, group_id):
    """
    Находит клуб по vk_group_id. Приоритет:
    1) group_id из события (если пришёл),
    2) _group_id самого бота (узнаём при старте — самый надёжный),
    3) первый клуб с непустым vk_group_id (запасной путь).
    """
    g = GlobalRepository(session)
    tenants = await g.list_tenants()
    gid = group_id or _group_id
    if gid:
        for t in tenants:
            if t.vk_group_id == gid:
                return t
    for t in tenants:
        if t.vk_group_id:
            return t
    return None


async def _is_full(svc, training) -> bool:
    active = await svc.repo.get_signups(training.id, "active")
    return len(active) >= training.max_participants


async def _show_list(user_id: int, group_id=None) -> None:
    """Отправляет карточки ближайших тренировок с кнопками записи."""
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None:
            await _send(user_id, "Клуб не привязан. Обратитесь к тренеру.")
            return
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        await _upsert_vk_user(svc, user_id)
        await session.commit()
        trainings = await svc.repo.list_upcoming()
        if not trainings:
            await _send(user_id, "Ближайших тренировок нет.", keyboard=_menu_kb())
            return
        for tr in trainings:
            card = await views.training_card_plain(svc, tr)
            full = await _is_full(svc, tr)
            await _send(user_id, card, keyboard=_kb(tr.id, full))


async def _do_signup(user_id: int, tid: int, group_id=None) -> str:
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None:
            return "Клуб не привязан."
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        name = await _upsert_vk_user(svc, user_id)
        await session.commit()
        res = await svc.sign_up(tid, PLATFORM, user_id, name)
        await session.commit()
        return {
            "active": "✅ Вы записаны!",
            "queue": f"⏳ Вы в очереди №{res.position}.",
            "already": "Вы уже записаны.",
            "closed": "Запись закрыта.",
        }.get(res.result, "Готово.")


async def _do_cancel(user_id: int, tid: int, group_id=None) -> str:
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None:
            return "Клуб не привязан."
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        res = await svc.cancel_signup(tid, PLATFORM, user_id,
                                      lock_minutes=tenant.cancel_lock_minutes)
        await session.commit()
        if res.get("locked"):
            return (f"Отмена закрыта: до тренировки меньше "
                    f"{res['lock_minutes']} мин.")
        return "Запись отменена." if res["cancelled"] else "Вы не были записаны."


async def _handle_text(user_id: int, text: str, group_id=None) -> None:
    """Обработка текстовых команд."""
    text = (text or "").strip().lower()
    if text in ("начать", "start", "список", "тренировки", "🏸 тренировки"):
        await _show_list(user_id, group_id)
    elif text.startswith("записаться "):
        try:
            tid = int(text.split()[1])
        except (IndexError, ValueError):
            await _send(user_id, "Формат: записаться <номер>"); return
        await _send(user_id, await _do_signup(user_id, tid, group_id))
    elif text.startswith("отмена "):
        try:
            tid = int(text.split()[1])
        except (IndexError, ValueError):
            await _send(user_id, "Формат: отмена <номер>"); return
        await _send(user_id, await _do_cancel(user_id, tid, group_id))
    elif text in ("профиль", "моя статистика", "👤 профиль"):
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, group_id)
            if tenant is None:
                await _send(user_id, "Клуб не привязан."); return
            svc = BookingService(session, tenant.id, tz=tenant.timezone)
            name = await _upsert_vk_user(svc, user_id)
            await session.commit()
            stats = await svc.user_stats(PLATFORM, user_id)
            await _send(user_id, profile_card(name, stats), keyboard=_menu_kb())
    elif text in ("стоп", "отписаться"):
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, group_id)
            if tenant is None:
                return
            svc = BookingService(session, tenant.id, tz=tenant.timezone)
            await svc.repo.set_subscription(PLATFORM, user_id, False)
            await session.commit()
            await _send(user_id, "Вы отписались от рассылки.")
    else:
        await _send(user_id,
                    "Команды:\n• Тренировки — ближайшие\n• Профиль — статистика\n"
                    "Или пользуйтесь кнопками.", keyboard=_menu_kb())


async def publish_to_wall(tenant_id: int, training_id: int) -> None:
    """
    Публикует анонс тренировки на стене VK-сообщества с кнопкой-ссылкой
    «Написать сообществу» (запись ведётся в личке с ботом).
    Тихо пропускает, если VK не настроен или у клуба нет vk_group_id.
    """
    if not _bot or not _enabled:
        return
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        tenant = await g.get_tenant(tenant_id)
        if not tenant or not tenant.vk_group_id:
            return
        svc = BookingService(session, tenant_id, tz=tenant.timezone)
        training = await svc.repo.get_training(training_id)
        if not training:
            return
        card = await views.training_card_plain(svc, training)
        group_id = tenant.vk_group_id

    text = ("📣 Новая тренировка — открыта запись!\n\n" + card +
            "\n\n✍️ Чтобы записаться, напишите сообществу «Тренировки».")
    # ссылка на диалог с сообществом
    link = f"https://vk.me/club{group_id}"
    try:
        await _bot.api.wall.post(
            owner_id=-group_id,          # отрицательный = сообщество
            from_group=1,                # от имени сообщества
            message=text,
            attachments=link)
        logger.info("VK: анонс тренировки %s опубликован на стене", training_id)
    except Exception as e:
        logger.warning("VK: не удалось опубликовать на стену: %s", e)


async def setup() -> None:
    global _bot, _enabled
    if _enabled:
        return
    if not settings.vk_token:
        logger.warning("VK_TOKEN не задан — VK отключён.")
        return
    try:
        from vkbottle import GroupEventType
        from vkbottle.bot import Bot, Message
    except ImportError:
        logger.warning("vkbottle не установлен — VK отключён.")
        return

    _bot = Bot(token=settings.vk_token)
    _enabled = True

    # узнаём id своего сообщества (надёжнее, чем искать наугад)
    global _group_id
    try:
        groups = await _bot.api.groups.get_by_id()
        # vkbottle может вернуть список или объект с .groups
        g0 = groups[0] if isinstance(groups, list) else groups.groups[0]
        _group_id = g0.id
        logger.info("VK: сообщество id=%s определено", _group_id)
    except Exception as e:
        logger.warning("VK: не удалось определить group_id: %s", e)

    @_bot.on.message()
    async def _on_message(message: Message):
        gid = getattr(message, "group_id", None)
        await _handle_text(message.from_id, message.text or "", gid)

    @_bot.on.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=dict)
    async def _on_callback(event: dict):
        obj = event.get("object", {})
        gid = event.get("group_id")
        user_id = obj.get("user_id")
        peer_id = obj.get("peer_id")
        event_id = obj.get("event_id")
        payload = obj.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        action = payload.get("a")
        tid = payload.get("tid")

        snackbar = "Готово"
        if action == "su":
            snackbar = await _do_signup(user_id, tid, gid)
        elif action == "cx":
            snackbar = await _do_cancel(user_id, tid, gid)
        elif action == "list":
            await _show_list(user_id, gid)
        elif action == "profile":
            await _handle_text(user_id, "профиль", gid)

        # ответ на нажатие (всплывающее уведомление) + обновляем карточку
        try:
            await _bot.api.messages.send_message_event_answer(
                event_id=event_id, user_id=user_id, peer_id=peer_id,
                event_data=json.dumps({"type": "show_snackbar",
                                       "text": snackbar[:90]}))
        except Exception as e:
            logger.warning("VK event answer error: %s", e)

    tasks.register_sender(PLATFORM, _send)
    logger.info("VK готов (кнопки + Long Poll).")


async def run_polling() -> None:
    if _bot:
        logger.info("VK: запускаю Long Poll…")
        await _bot.run_polling()


async def feed_callback_event(body: dict) -> None:
    """Обработка события VK Callback API (webhook)."""
    if not _enabled:
        return
    t = body.get("type")
    if t == "message_new":
        obj = body.get("object", {}).get("message", {})
        await _handle_text(obj.get("from_id"), obj.get("text") or "",
                           body.get("group_id"))
