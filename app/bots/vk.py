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


def _kb(tid: int, is_full: bool = False, is_admin: bool = False) -> str:
    """Inline-клавиатура под карточкой тренировки.
    Участнику: записаться/отмена. Админу — плюс управление."""
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    signup = "⏳ Встать в очередь" if is_full else "✅ Записаться"
    kb = Keyboard(inline=True)
    kb.add(Callback(signup, payload={"a": "su", "tid": tid}),
           color=KeyboardButtonColor.POSITIVE)
    kb.add(Callback("❌ Отменить", payload={"a": "cx", "tid": tid}),
           color=KeyboardButtonColor.NEGATIVE)
    if is_admin:
        kb.row()
        kb.add(Callback("✅ Явка/оплата", payload={"a": "att", "tid": tid}),
               color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Callback("👤 Гость", payload={"a": "guest", "tid": tid}))
        kb.add(Callback("🗑 Удалить", payload={"a": "deltr", "tid": tid}),
               color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def _confirm_del_kb(tid: int) -> str:
    """Кнопки подтверждения отмены тренировки: Да / Нет."""
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    kb = Keyboard(inline=True)
    kb.add(Callback("✅ Да, отменить", payload={"a": "deltr_yes", "tid": tid}),
           color=KeyboardButtonColor.NEGATIVE)
    kb.add(Callback("↩️ Нет", payload={"a": "deltr_no", "tid": tid}),
           color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def _menu_kb(is_admin: bool = False) -> str:
    """Постоянное меню снизу: список / мои записи / профиль (+ создать)."""
    from vkbottle import Keyboard, Text, KeyboardButtonColor
    kb = Keyboard(inline=False, one_time=False)
    kb.add(Text("🏸 Тренировки", payload={"a": "list"}))
    kb.add(Text("📅 Мои записи", payload={"a": "my"}))
    kb.row()
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


async def _show_my(user_id: int, group_id=None) -> None:
    """Показывает тренировки, на которые записан пользователь, с кнопкой отмены."""
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None:
            await _send(user_id, "Клуб не привязан."); return
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        rows = await svc.my_trainings(PLATFORM, user_id)
        is_admin = tenant.admin_vk_id == user_id
        if not rows:
            await _send(user_id, "📭 Вы не записаны ни на одну тренировку.\n"
                        "Нажмите «🏸 Тренировки», чтобы записаться.",
                        keyboard=_menu_kb(is_admin))
            return
        for training, status, position in rows:
            card = await views.training_card_plain(svc, training)
            mark = ("✅ Вы записаны" if status == "active"
                    else f"⏳ Вы в очереди (№{position})")
            full = await _is_full(svc, training)
            await _send(user_id, f"{mark}\n\n{card}",
                        keyboard=_kb(training.id, full, is_admin))
        await _send(user_id, "⌨️ Меню внизу 👇", keyboard=_menu_kb(is_admin))


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
        is_admin = tenant.admin_vk_id == user_id
        trainings = await svc.repo.list_upcoming()
        if not trainings:
            await _send(user_id, "Ближайших тренировок нет.",
                        keyboard=_menu_kb(is_admin))
            return
        for tr in trainings:
            card = await views.training_card_plain(svc, tr)
            full = await _is_full(svc, tr)
            await _send(user_id, card, keyboard=_kb(tr.id, full, is_admin))
        # закрепляем нижнее меню отдельным коротким сообщением
        await _send(user_id, "⌨️ Меню внизу 👇", keyboard=_menu_kb(is_admin))


async def _edit_card(peer_id: int, cmid: int, tid: int, group_id=None,
                     admin_uid: int | None = None) -> None:
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
        is_admin = admin_uid is not None and tenant.admin_vk_id == admin_uid
    try:
        await _bot.api.messages.edit(
            peer_id=peer_id,
            conversation_message_id=cmid,
            message=card,
            keyboard=_kb(tid, full, is_admin))
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


# ─────────── Админские действия (только тренер) ───────────

def _attendance_kb(tid: int, signups: list) -> str:
    """Клавиатура для отметки явки/оплаты: по кнопке на участника."""
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    kb = Keyboard(inline=True)
    for i, s in enumerate(signups[:8]):   # VK-лимит на кнопки
        att = "✅" if s.attended else "⬜"
        paid = "💰" if s.paid else "⬜"
        kb.add(Callback(f"{att}{paid} {s.name[:18]}",
                        payload={"a": "att_t", "sid": s.id, "tid": tid}),
               color=KeyboardButtonColor.PRIMARY)
        if i % 1 == 0:
            kb.row()
    kb.add(Callback("← Назад", payload={"a": "att_close", "tid": tid}))
    return kb.get_json()


async def _show_attendance(user_id: int, tid: int, group_id=None) -> None:
    """Показывает список участников с кнопками отметки явки/оплаты (админ)."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        training = await svc.repo.get_training(tid)
        if not training:
            await _send(user_id, "Тренировка не найдена."); return
        active = await svc.repo.get_signups(tid, "active")
        title = training.title
    if not active:
        await _send(user_id, f"«{title}» — пока никто не записан.")
        return
    legend = ("Отметка явки/оплаты для «" + title + "»:\n"
              "✅ = пришёл, 💰 = оплатил (жмите, чтобы переключить)")
    await _send(user_id, legend, keyboard=_attendance_kb(tid, active))


async def _refresh_attendance(peer_id: int, cmid: int, tid: int,
                              group_id=None) -> None:
    """Обновляет сообщение со списком явки/оплаты после переключения."""
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
        active = await svc.repo.get_signups(tid, "active")
        title = training.title
    if not active:
        return
    legend = ("Отметка явки/оплаты для «" + title + "»:\n"
              "✅ = пришёл, 💰 = оплатил (жмите, чтобы переключить)")
    try:
        await _bot.api.messages.edit(
            peer_id=peer_id, conversation_message_id=cmid,
            message=legend, keyboard=_attendance_kb(tid, active))
    except Exception as e:
        logger.warning("VK: не удалось обновить явку: %s", e)


async def _toggle_att(user_id: int, sid: int, group_id=None) -> str:
    """Переключает явку И оплату по кругу: ничего → явка → +оплата → сброс."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            return "⛔ Только тренер."
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        s = await svc.repo.get_signup_by_id(sid)
        if not s:
            return "Не найдено."
        # цикл состояний: (нет,нет)→(да,нет)→(да,да)→(нет,нет)
        if not s.attended and not s.paid:
            s.attended = True
        elif s.attended and not s.paid:
            s.paid = True
        else:
            s.attended = False; s.paid = False
        await session.commit()
        state = ("пришёл + оплатил" if s.attended and s.paid
                 else "пришёл" if s.attended else "сброшено")
        return f"{s.name}: {state}"


async def _delete_training(user_id: int, tid: int, group_id=None) -> str:
    """Отменяет тренировку (уведомляет записанных + Telegram-группу). Только админ."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            return "⛔ Только тренер."
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        training = await svc.repo.get_training(tid)
        if not training:
            return "Тренировка не найдена."
        if getattr(training, "is_cancelled", False):
            return "Эта тренировка уже отменена."
        title = training.title
        when = svc.format_local(training.start_at)
        tenant_id = tenant.id
        await svc.cancel_training(tid)
    # уведомляем Telegram-группу об отмене
    try:
        from app.bots import telegram
        await telegram.notify_group_cancelled(tenant_id, title, when)
    except Exception as e:
        logger.warning("VK: не удалось уведомить Telegram-группу: %s", e)
    return f"🗑 Тренировка «{title}» отменена, участники уведомлены."


# ─────────── Пошаговое создание тренировки (кнопки, только админ) ───────────
import datetime as _dt

_WD_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _kb_from(options: list[tuple[str, dict]], add_manual: bool = True,
             per_row: int = 2) -> str:
    """Строит inline-клавиатуру из списка (label, payload). +«Ввести вручную»."""
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    kb = Keyboard(inline=True)
    for i, (label, payload) in enumerate(options):
        kb.add(Callback(label, payload=payload))
        if (i + 1) % per_row == 0 and i + 1 < len(options):
            kb.row()
    if add_manual:
        kb.row()
        kb.add(Callback("✏️ Ввести вручную", payload={"a": "cr_manual"}),
               color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Callback("❌ Отмена", payload={"a": "create_cancel"}),
           color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def _date_options(tz) -> list[tuple[str, dict]]:
    today = _dt.datetime.now(tz).date()
    opts = [("Сегодня", {"a": "cr_date", "v": today.isoformat()}),
            ("Завтра", {"a": "cr_date", "v": (today + _dt.timedelta(1)).isoformat()})]
    for i in range(2, 7):
        d = today + _dt.timedelta(days=i)
        opts.append((f"{_WD_RU[d.weekday()]} {d.day:02d}.{d.month:02d}",
                     {"a": "cr_date", "v": d.isoformat()}))
    return opts


def _time_options() -> list[tuple[str, dict]]:
    return [(t, {"a": "cr_time", "v": t})
            for t in ("18:00", "19:00", "20:00", "21:00", "10:00", "12:00")]


def _duration_options() -> list[tuple[str, dict]]:
    return [("1 ч", {"a": "cr_dur", "v": 60}), ("1.5 ч", {"a": "cr_dur", "v": 90}),
            ("2 ч", {"a": "cr_dur", "v": 120}), ("3 ч", {"a": "cr_dur", "v": 180})]


def _price_options() -> list[tuple[str, dict]]:
    return [("Бесплатно", {"a": "cr_price", "v": 0}),
            ("300₽", {"a": "cr_price", "v": 300}), ("500₽", {"a": "cr_price", "v": 500}),
            ("700₽", {"a": "cr_price", "v": 700}), ("800₽", {"a": "cr_price", "v": 800})]


def _max_options() -> list[tuple[str, dict]]:
    return [(str(n), {"a": "cr_max", "v": n}) for n in (2, 4, 6, 8, 10, 12)]


async def _location_options(svc) -> list[tuple[str, dict]]:
    """Последние использованные места (до 4)."""
    try:
        places = await svc.recent_locations(limit=4)
    except Exception:
        places = []
    return [(p[:24], {"a": "cr_loc", "v": p}) for p in places]


async def _start_create(user_id: int, group_id=None) -> None:
    """Начинает диалог создания (проверяет права админа)."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Создавать тренировки может только тренер.")
            return
    _fsm[user_id] = {"step": "title", "data": {}, "gid": group_id, "manual": False}
    await _send(user_id, "📝 Введите название тренировки:", keyboard=_cancel_kb())


async def _cr_ask(user_id: int, step: str, gid) -> None:
    """Показывает вопрос текущего шага с кнопками вариантов."""
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        tz = tenant.timezone if tenant else "Europe/Moscow"
        svc = BookingService(session, tenant.id, tz=tz)
        if step == "date":
            await _send(user_id, "📅 Выберите дату:",
                        keyboard=_kb_from(_date_options(svc.tz)))
        elif step == "time":
            await _send(user_id, "🕐 Выберите время:",
                        keyboard=_kb_from(_time_options(), per_row=4))
        elif step == "location":
            opts = await _location_options(svc)
            if opts:
                await _send(user_id, "📍 Выберите место или введите вручную:",
                            keyboard=_kb_from(opts, per_row=1))
            else:
                _fsm[user_id]["manual"] = True
                await _send(user_id, "📍 Введите место (зал/адрес):",
                            keyboard=_cancel_kb())
        elif step == "duration":
            await _send(user_id, "⏱ Выберите длительность:",
                        keyboard=_kb_from(_duration_options(), per_row=4))
        elif step == "price":
            await _send(user_id, "💰 Выберите цену:",
                        keyboard=_kb_from(_price_options(), per_row=3))
        elif step == "max":
            await _send(user_id, "👥 Максимум участников:",
                        keyboard=_kb_from(_max_options(), per_row=3))


# порядок шагов
_CR_ORDER = ["title", "date", "time", "location", "duration", "price", "max"]


async def _cr_advance(user_id: int, gid) -> None:
    """Переходит к следующему шагу или финализирует."""
    state = _fsm.get(user_id)
    if not state:
        return
    cur = state["step"]
    nxt = _CR_ORDER[_CR_ORDER.index(cur) + 1] if cur in _CR_ORDER[:-1] else None
    if nxt is None:
        await _finalize_create(user_id, state)
        return
    state["step"] = nxt
    state["manual"] = False
    await _cr_ask(user_id, nxt, gid)


async def _cr_callback(user_id: int, payload: dict, gid,
                       peer_id=None, cmid=None) -> None:
    """Обработка нажатий кнопок в мастере создания."""
    state = _fsm.get(user_id)
    if not state:
        return
    a = payload.get("a")
    v = payload.get("v")
    data = state["data"]

    if a == "cr_manual":
        state["manual"] = True
        prompts = {
            "date": "Введите дату: ДД.ММ.ГГГГ (напр. 20.07.2026)",
            "time": "Введите время: ЧЧ:ММ (напр. 19:30)",
            "location": "Введите место (зал/адрес):",
            "duration": "Введите длительность в минутах (напр. 90):",
            "price": "Введите цену в рублях (напр. 500 или 0):",
            "max": "Введите максимум участников (напр. 6):",
        }
        # убираем кнопки у текущего сообщения
        await _strip_buttons(peer_id, cmid, "✏️ Ввод вручную…")
        await _send(user_id, prompts.get(state["step"], "Введите значение:"),
                    keyboard=_cancel_kb())
        return

    label = None
    if a == "cr_date":
        data["date"] = v; label = f"✅ Дата: {_fmt_date(v)}"
    elif a == "cr_time":
        data["time"] = v; label = f"✅ Время: {v}"
    elif a == "cr_loc":
        data["location"] = v; label = f"✅ Место: {v}"
    elif a == "cr_dur":
        data["duration_min"] = int(v)
        label = f"✅ Длительность: {int(v)} мин"
    elif a == "cr_price":
        data["price_minor"] = int(v) * 100
        label = f"✅ Цена: {v}₽" if int(v) else "✅ Бесплатно"
    elif a == "cr_max":
        data["max_participants"] = int(v); label = f"✅ Максимум: {v}"
    # переписываем нажатое сообщение — кнопки исчезают, остаётся выбор
    if label:
        await _strip_buttons(peer_id, cmid, label)
    await _cr_advance(user_id, gid)


async def _strip_buttons(peer_id, cmid, text: str) -> None:
    """Убирает кнопки у сообщения, заменяя его коротким текстом-следом."""
    if not _bot or not peer_id or not cmid:
        return
    try:
        await _bot.api.messages.edit(
            peer_id=peer_id, conversation_message_id=cmid,
            message=text, keyboard="")
    except Exception:
        pass


async def _fsm_process(user_id: int, text: str) -> bool:
    """Обрабатывает ТЕКСТОВЫЙ ввод в мастере (название и «вручную»)."""
    state = _fsm.get(user_id)
    if state is None:
        return False

    text = (text or "").strip()
    if text.lower() in ("отмена", "❌ отмена", "стоп"):
        _fsm.pop(user_id, None)
        await _send(user_id, "Отменено.", keyboard=_menu_kb(True))
        return True

    # мини-диалог добавления гостя
    if state.get("kind") == "guest":
        _fsm.pop(user_id, None)
        if not text:
            await _send(user_id, "Имя пустое.", keyboard=_menu_kb(True)); return True
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, state["gid"])
            svc = BookingService(session, tenant.id, tz=tenant.timezone)
            res = await svc.sign_up_guest(state["tid"], text, added_by=user_id)
            await session.commit()
        msg = {"active": f"✅ Гость «{text}» записан.",
               "queue": f"⏳ Гость «{text}» в очереди.",
               "closed": "Запись закрыта."}.get(
                   getattr(res, "result", "active"), f"Гость «{text}» добавлен.")
        await _send(user_id, msg, keyboard=_menu_kb(True))
        return True

    step = state["step"]
    gid = state["gid"]
    data = state["data"]

    # название — всегда текстом
    if step == "title":
        if not text:
            await _send(user_id, "Название пустое. Введите ещё раз:",
                        keyboard=_cancel_kb()); return True
        data["title"] = text
        state["step"] = "date"
        await _cr_ask(user_id, "date", gid)
        return True

    # остальные шаги текстом — только если выбран режим «вручную»
    if not state.get("manual"):
        # пользователь пишет текст, хотя ждём кнопку — подскажем
        await _send(user_id, "Пожалуйста, выберите вариант кнопкой "
                    "или нажмите «✏️ Ввести вручную».")
        return True

    if step == "date":
        try:
            d = _dt.datetime.strptime(text, "%d.%m.%Y").date()
        except ValueError:
            await _send(user_id, "Формат даты: ДД.ММ.ГГГГ (напр. 20.07.2026)",
                        keyboard=_cancel_kb()); return True
        data["date"] = d.isoformat()
    elif step == "time":
        try:
            _dt.datetime.strptime(text, "%H:%M")
        except ValueError:
            await _send(user_id, "Формат времени: ЧЧ:ММ (напр. 19:30)",
                        keyboard=_cancel_kb()); return True
        data["time"] = text
    elif step == "location":
        data["location"] = text or "—"
    elif step == "duration":
        if not text.isdigit() or int(text) <= 0:
            await _send(user_id, "Введите число минут (напр. 90):",
                        keyboard=_cancel_kb()); return True
        data["duration_min"] = int(text)
    elif step == "price":
        if not text.isdigit():
            await _send(user_id, "Введите число рублей (напр. 500 или 0):",
                        keyboard=_cancel_kb()); return True
        data["price_minor"] = int(text) * 100
    elif step == "max":
        if not text.isdigit() or int(text) <= 0:
            await _send(user_id, "Введите число участников (напр. 6):",
                        keyboard=_cancel_kb()); return True
        data["max_participants"] = int(text)

    await _cr_advance(user_id, gid)
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
        # собираем дату+время
        start_at = svc.parse_local(f"{_fmt_date(data['date'])} {data['time']}")
        training = await svc.create_training(
            title=data["title"], start_at=start_at,
            location=data.get("location", "—"),
            max_participants=data["max_participants"],
            duration_min=data["duration_min"], state="published",
            publish_at=None, platform=PLATFORM, user_id=user_id)
        if data.get("price_minor"):
            training.price_minor = data["price_minor"]
        await session.commit()
        card = await views.training_card_plain(svc, training)
        tid = training.id
        full = await _is_full(svc, training)
        subs = await svc.repo.get_subscribers()
        when = svc.format_local(training.start_at)
        note = (f"🏸 Открыта запись на «{training.title}»\n📅 {when}"
                + (f"\n📍 {training.location}" if training.location else ""))
        for sub in subs:
            if sub.user_id == user_id and sub.platform == PLATFORM:
                continue
            await svc.repo.enqueue(sub.platform, sub.user_id, note)
        await session.commit()
    await _send(user_id, "✅ Тренировка создана!", keyboard=_menu_kb(True))
    await _send(user_id, card, keyboard=_kb(tid, full, True))
    try:
        from app.bots import telegram
        await telegram._publish_to_group(tenant.id, tid)
    except Exception as e:
        logger.warning("VK: публикация в Telegram-группу не удалась: %s", e)
    try:
        await publish_to_wall(tenant.id, tid)
    except Exception as e:
        logger.warning("VK: анонс на стену не удался: %s", e)


def _fmt_date(iso: str) -> str:
    """2026-07-20 -> 20.07.2026 (для parse_local)."""
    d = _dt.date.fromisoformat(iso)
    return f"{d.day:02d}.{d.month:02d}.{d.year}"





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
    elif text in ("мои записи", "📅 мои записи", "моя тренировка", "мои"):
        await _show_my(user_id, group_id)
    elif text in ("создать", "➕ создать тренировку", "новая тренировка"):
        await _start_create(user_id, group_id)
    elif text in ("мой id", "мойid", "id", "мой айди"):
        await _send(user_id, f"Ваш VK ID: {user_id}\n\n"
                    "Передайте его тренеру, чтобы он назначил вас "
                    "администратором (если нужно).")
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
        sid = payload.get("sid")

        snackbar = "Готово"
        if action == "su":
            snackbar = await _do_signup(user_id, tid, gid)
            await _edit_card(peer_id, cmid, tid, gid, user_id)
        elif action == "cx":
            snackbar = await _do_cancel(user_id, tid, gid)
            await _edit_card(peer_id, cmid, tid, gid, user_id)
        elif action == "list":
            await _show_list(user_id, gid)
        elif action == "my":
            await _show_my(user_id, gid)
        elif action == "profile":
            await _handle_text(user_id, "профиль", gid)
        elif action == "create":
            await _start_create(user_id, gid)
        elif action == "create_cancel":
            _fsm.pop(user_id, None)
            await _send(user_id, "Создание отменено.", keyboard=_menu_kb(True))
        elif action in ("cr_date", "cr_time", "cr_loc", "cr_dur",
                        "cr_price", "cr_max", "cr_manual"):
            await _cr_callback(user_id, payload, gid, peer_id, cmid)
        elif action == "att":                       # открыть явку/оплату
            await _show_attendance(user_id, tid, gid)
        elif action == "att_t":                     # переключить у участника
            snackbar = await _toggle_att(user_id, sid, gid)
            await _refresh_attendance(peer_id, cmid, tid, gid)
        elif action == "att_close":                 # закрыть явку
            snackbar = "Готово"
        elif action == "guest":                     # добавить гостя
            async with SessionLocal() as session:
                if not await _is_admin_vk(session, user_id, gid):
                    snackbar = "⛔ Только тренер"
                else:
                    _fsm[user_id] = {"kind": "guest", "tid": tid, "gid": gid}
                    await _send(user_id,
                                "👤 Введите имя гостя:", keyboard=_cancel_kb())
        elif action == "deltr":                     # спросить подтверждение
            async with SessionLocal() as session:
                if not await _is_admin_vk(session, user_id, gid):
                    snackbar = "⛔ Только тренер"
                else:
                    tenant = await _resolve_tenant(session, gid)
                    svc = BookingService(session, tenant.id, tz=tenant.timezone)
                    tr = await svc.repo.get_training(tid)
                    title = tr.title if tr else "?"
                    await _send(user_id,
                                f"❓ Точно отменить тренировку «{title}»?\n"
                                "Все записанные получат уведомление.",
                                keyboard=_confirm_del_kb(tid))
        elif action == "deltr_yes":                 # подтверждено — отменяем
            # сначала убираем кнопки у вопроса, чтобы не нажали дважды
            if cmid:
                try:
                    await _bot.api.messages.edit(
                        peer_id=peer_id, conversation_message_id=cmid,
                        message="⏳ Отменяю тренировку…", keyboard="")
                except Exception:
                    pass
            result = await _delete_training(user_id, tid, gid)
            snackbar = result
            # переписываем сообщение на итог (без кнопок) + отдельное уведомление
            if cmid:
                try:
                    await _bot.api.messages.edit(
                        peer_id=peer_id, conversation_message_id=cmid,
                        message=result, keyboard="")
                except Exception:
                    await _send(user_id, result, keyboard=_menu_kb(True))
            else:
                await _send(user_id, result, keyboard=_menu_kb(True))
        elif action == "deltr_no":                  # передумал
            snackbar = "Отмена отменена 🙂"
            if cmid:
                try:
                    await _bot.api.messages.edit(
                        peer_id=peer_id, conversation_message_id=cmid,
                        message="↩️ Отмена тренировки прервана.", keyboard="")
                except Exception:
                    pass

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
