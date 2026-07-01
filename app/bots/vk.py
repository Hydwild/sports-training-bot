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
_fsm: dict[int, dict] = {}   # user_id -> {step, data} — диалог создания тренировки


async def _is_admin_vk(session, user_id: int, group_id=None) -> bool:
    """Проверяет, что пользователь — админ клуба (по admin_vk_id)."""
    tenant = await _resolve_tenant(session, group_id)
    if tenant is None:
        return False
    return tenant.admin_vk_id == user_id


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


def _menu_kb(is_admin: bool = False) -> str:
    """Постоянное меню снизу: список / профиль (+ создать для админа)."""
    from vkbottle import Keyboard, Text, KeyboardButtonColor
    kb = Keyboard(inline=False, one_time=False)
    kb.add(Text("🏸 Тренировки", payload={"a": "list"}))
    kb.add(Text("👤 Профиль", payload={"a": "profile"}))
    if is_admin:
        kb.row()
        kb.add(Text("➕ Создать тренировку", payload={"a": "create"}),
               color=KeyboardButtonColor.POSITIVE)
    return kb.get_json()


def _cancel_kb() -> str:
    """Кнопка отмены во время пошагового создания."""
    from vkbottle import Keyboard, Text, KeyboardButtonColor
    kb = Keyboard(inline=False, one_time=False)
    kb.add(Text("❌ Отмена", payload={"a": "create_cancel"}),
           color=KeyboardButtonColor.NEGATIVE)
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
        # закрепляем нижнее меню отдельным коротким сообщением
        await _send(user_id, "⌨️ Меню внизу 👇", keyboard=_menu_kb())


async def _edit_card(peer_id: int, cmid: int, tid: int, group_id=None) -> None:
    """
    Живое обновление: переписывает карточку тренировки на месте
    (новый счётчик, список, кнопка) после записи/отмены.
    """
    if not _bot or not cmid:
        return
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None:
            return
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        training = await svc.repo.get_training(tid)
        if not training:
            return
        card = await views.training_card_plain(svc, training)
        full = await _is_full(svc, training)
    try:
        await _bot.api.messages.edit(
            peer_id=peer_id,
            conversation_message_id=cmid,
            message=card,
            keyboard=_kb(tid, full))
    except Exception as e:
        logger.warning("VK: не удалось обновить карточку: %s", e)


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


# ─────────── Пошаговое создание тренировки (только админ) ───────────
_STEPS = ["title", "date", "location", "duration", "price", "max"]
_PROMPTS = {
    "title": "📝 Введите название тренировки:",
    "date": "📅 Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n(например 05.07.2026 19:00):",
    "location": "📍 Введите место (зал/адрес):",
    "duration": "⏱ Введите длительность в минутах (например 90):",
    "price": "💰 Введите цену в рублях (например 500, или 0 если бесплатно):",
    "max": "👥 Введите максимум участников (например 6):",
}


async def _start_create(user_id: int, group_id=None) -> None:
    """Начинает диалог создания тренировки (проверяет права админа)."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Создавать тренировки может только тренер.")
            return
    _fsm[user_id] = {"step": 0, "data": {}, "gid": group_id}
    await _send(user_id, _PROMPTS["title"], keyboard=_cancel_kb())


async def _fsm_process(user_id: int, text: str) -> bool:
    """
    Обрабатывает шаг диалога создания. Возвращает True, если сообщение
    было частью диалога (и обработано здесь).
    """
    state = _fsm.get(user_id)
    if state is None:
        return False

    text = (text or "").strip()
    if text.lower() in ("отмена", "❌ отмена", "стоп"):
        _fsm.pop(user_id, None)
        await _send(user_id, "Создание отменено.", keyboard=_menu_kb(True))
        return True

    step_name = _STEPS[state["step"]]
    data = state["data"]

    # валидация текущего шага
    if step_name == "title":
        if not text:
            await _send(user_id, "Название не может быть пустым. Введите ещё раз:",
                        keyboard=_cancel_kb()); return True
        data["title"] = text
    elif step_name == "date":
        # проверим формат через сервис
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, state["gid"])
            svc = BookingService(session, tenant.id, tz=tenant.timezone)
            when = svc.parse_local(text)
        if when is None:
            await _send(user_id, "Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ\n"
                        "например 05.07.2026 19:00. Введите ещё раз:",
                        keyboard=_cancel_kb()); return True
        data["start_at"] = when
    elif step_name == "location":
        data["location"] = text or "—"
    elif step_name == "duration":
        if not text.isdigit() or int(text) <= 0:
            await _send(user_id, "Введите число минут (например 90):",
                        keyboard=_cancel_kb()); return True
        data["duration_min"] = int(text)
    elif step_name == "price":
        if not text.isdigit():
            await _send(user_id, "Введите число рублей (например 500 или 0):",
                        keyboard=_cancel_kb()); return True
        data["price_minor"] = int(text) * 100
    elif step_name == "max":
        if not text.isdigit() or int(text) <= 0:
            await _send(user_id, "Введите число участников (например 6):",
                        keyboard=_cancel_kb()); return True
        data["max_participants"] = int(text)

    # переход к следующему шагу или финал
    state["step"] += 1
    if state["step"] < len(_STEPS):
        nxt = _STEPS[state["step"]]
        await _send(user_id, _PROMPTS[nxt], keyboard=_cancel_kb())
        return True

    # все шаги пройдены — создаём тренировку
    await _finalize_create(user_id, state)
    return True


async def _finalize_create(user_id: int, state: dict) -> None:
    data = state["data"]
    gid = state["gid"]
    _fsm.pop(user_id, None)
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        if tenant is None:
            await _send(user_id, "Клуб не привязан."); return
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        training = await svc.create_training(
            title=data["title"], start_at=data["start_at"],
            location=data["location"], max_participants=data["max_participants"],
            duration_min=data["duration_min"], state="published",
            publish_at=None, platform=PLATFORM, user_id=user_id)
        if data.get("price_minor"):
            training.price_minor = data["price_minor"]
        await session.commit()
        card = await views.training_card_plain(svc, training)
        tid = training.id
        full = await _is_full(svc, training)
    await _send(user_id, "✅ Тренировка создана!", keyboard=_menu_kb(True))
    await _send(user_id, card, keyboard=_kb(tid, full))
    # публикуем анонс на стену и уведомляем Telegram-подписчиков
    try:
        await publish_to_wall(tenant.id, tid)
    except Exception as e:
        logger.warning("VK: анонс на стену не удался: %s", e)


async def _handle_text(user_id: int, text: str, group_id=None) -> None:
    """Обработка текстовых команд."""
    raw = (text or "").strip()
    # если идёт пошаговое создание — направляем ввод туда (нужен оригинал)
    if user_id in _fsm:
        if await _fsm_process(user_id, raw):
            return
    text = raw.lower()
    if text in ("начать", "start", "список", "тренировки", "🏸 тренировки"):
        await _show_list(user_id, group_id)
    elif text in ("создать", "➕ создать тренировку", "новая тренировка"):
        await _start_create(user_id, group_id)
    elif text in ("привет", "здравствуйте", "меню", "помощь", "help", "/start"):
        async with SessionLocal() as session:
            is_admin = await _is_admin_vk(session, user_id, group_id)
        await _send(user_id,
                    "👋 Привет! Я бот клуба.\n\n"
                    "🏸 Тренировки — посмотреть ближайшие и записаться\n"
                    "👤 Профиль — ваша статистика\n\n"
                    "Пользуйтесь кнопками меню внизу 👇",
                    keyboard=_menu_kb(is_admin))
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
        card = await views.announce_card_plain(svc, training)
        group_id = tenant.vk_group_id

    # ссылку кладём прямо в текст: как attachment VK требует фото-превью
    link = f"https://vk.me/club{group_id}"
    text = ("📣 Новая тренировка — открыта запись!\n\n" + card +
            "\n\n✍️ Чтобы записаться, напишите сообществу в личные сообщения "
            f"(команда «Тренировки»):\n{link}")
    try:
        await _bot.api.wall.post(
            owner_id=-group_id,          # отрицательный = сообщество
            from_group=1,                # от имени сообщества
            message=text)
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
        cmid = obj.get("conversation_message_id")
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
            await _edit_card(peer_id, cmid, tid, gid)   # живое обновление
        elif action == "cx":
            snackbar = await _do_cancel(user_id, tid, gid)
            await _edit_card(peer_id, cmid, tid, gid)   # живое обновление
        elif action == "list":
            await _show_list(user_id, gid)
        elif action == "profile":
            await _handle_text(user_id, "профиль", gid)
        elif action == "create":
            await _start_create(user_id, gid)
        elif action == "create_cancel":
            _fsm.pop(user_id, None)
            await _send(user_id, "Создание отменено.", keyboard=_menu_kb(True))

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
