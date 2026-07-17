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
from app.core.features import features
from app.db.engine import SessionLocal
from app.repositories.repo import GlobalRepository
from app.services import tasks
from app.services.booking import BookingService

logger = logging.getLogger("vk")
PLATFORM = "vk"

_bot = None   # type: ignore
_enabled = False
_group_id = None   # id сообщества, к которому привязан токен
_fsm: dict[int, dict] = {}   # user_id -> {step, data, ...} — активные диалоги
_FSM_TTL_SEC = 3600          # незавершённые диалоги живут не дольше часа


def _fsm_gc() -> None:
    """Удаляет заброшенные диалоги старше TTL — защита от утечки памяти.
    Вызывается при старте каждого нового диалога."""
    import time
    now = time.time()
    stale = [uid for uid, st in _fsm.items()
             if now - st.get("_ts", now) > _FSM_TTL_SEC]
    for uid in stale:
        _fsm.pop(uid, None)


def _fsm_set(user_id: int, state: dict) -> None:
    """Ставит состояние диалога с меткой времени и чистит старые."""
    import time
    _fsm_gc()
    state["_ts"] = time.time()
    _fsm[user_id] = state


async def _is_admin_vk(session, user_id: int, group_id=None) -> bool:
    """Проверяет, что пользователь — админ клуба (по admin_vk_id)."""
    tenant = await _resolve_tenant(session, group_id)
    if tenant is None:
        return False
    return tenant.admin_vk_id == user_id


# ─── мультиклиент: боты клубов с собственными VK-токенами ───
import contextvars as _cv

_ctx_api = _cv.ContextVar("vk_api", default=None)   # api текущего события
_api_by_tenant: dict[int, object] = {}              # tenant_id -> API
_client_bots: dict = {}                             # tenant_id -> Bot
_client_tasks: dict[int, "object"] = {}             # tenant_id -> asyncio.Task
_client_tokens: dict[int, str] = {}                 # tenant_id -> vk_token
_vk_polling_active = False


def _api():
    """API для текущего контекста: событие клиентского бота -> его api,
    иначе бот по умолчанию (из env)."""
    api = _ctx_api.get()
    if api is not None:
        return api
    return _bot.api if _bot else None


async def _send(user_id: int, text: str, keyboard: str | None = None,
                tenant_id: int | None = None) -> None:
    api = (_api_by_tenant.get(tenant_id) if tenant_id is not None else None)         or _api()
    if api:
        await api.messages.send(
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
    kb.row()
    kb.add(Callback("🔄 Обновить список", payload={"a": "refresh", "tid": tid}))
    if is_admin:
        kb.row()
        kb.add(Callback("✅ Явка/оплата", payload={"a": "att", "tid": tid}),
               color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Callback("✏️ Изменить", payload={"a": "edit", "tid": tid}))
        kb.add(Callback("👤 Гость", payload={"a": "guest", "tid": tid}))
        kb.row()
        kb.add(Callback("🔁 Повторить", payload={"a": "rep", "tid": tid}))
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


def _menu_kb(is_admin: bool = False, more: bool = False) -> str:
    """Меню снизу. У админа два экрана: основной и «⋯ Ещё»."""
    from vkbottle import Keyboard, Text, KeyboardButtonColor
    kb = Keyboard(inline=False, one_time=False)
    if not is_admin:
        kb.add(Text("🏸 Тренировки", payload={"a": "list"}))
        kb.add(Text("📅 Мои записи", payload={"a": "my"}))
        kb.row()
        kb.add(Text("👤 Профиль", payload={"a": "profile"}))
        kb.add(Text("🏆 Рейтинг", payload={"a": "rating"}))
    elif not more:
        kb.add(Text("➕ Создать тренировку", payload={"a": "create"}),
               color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text("🏸 Тренировки", payload={"a": "list"}))
        kb.add(Text("📆 Расписание", payload={"a": "sched"}))
        kb.row()
        kb.add(Text("📢 Рассылка", payload={"a": "bcast"}))
        kb.add(Text("👤 Записать гостя", payload={"a": "guest_pick"}))
        kb.row()
        kb.add(Text("✅ Явки", payload={"a": "att_pick"}))
        kb.add(Text("⋯ Ещё", payload={"a": "menu_more"}))
    else:
        kb.add(Text("📅 Мои записи", payload={"a": "my"}))
        kb.add(Text("🏆 Рейтинг", payload={"a": "rating"}))
        kb.row()
        kb.add(Text("👤 Профиль", payload={"a": "profile"}))
        kb.add(Text("📊 Статистика", payload={"a": "stats"}))
        kb.row()
        kb.add(Text("✏️ Имена", payload={"a": "names"}))
        kb.add(Text("⏰ Напоминание", payload={"a": "rem"}))
        kb.row()
        kb.add(Text("⬅️ Назад", payload={"a": "menu_back"}),
               color=KeyboardButtonColor.PRIMARY)
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
        profile = await fetch_vk_profile(_api(), user_id)
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
    1) group_id из события (если пришёл) или _group_id самого бота
       (узнаём при старте) — если он известен, ищем ТОЛЬКО точное
       совпадение. Если совпадения нет — клуб не определён (не гадаем),
       иначе при временном сбое определения group_id можно было бы
       случайно отдать данные чужого клуба в мультиклиентной установке.
    2) Если group_id вообще неизвестен (редкий случай) и во всей базе
       есть РОВНО один клуб с заданным vk_group_id — это точно он,
       единственный кандидат, тут гадать не нужно. При двух и более
       кандидатах — тоже не гадаем, возвращаем None.
    """
    g = GlobalRepository(session)
    tenants = await g.list_tenants()
    gid = group_id or _group_id
    if gid:
        for t in tenants:
            if t.vk_group_id == gid:
                return t
        return None
    with_vk = [t for t in tenants if t.vk_group_id]
    return with_vk[0] if len(with_vk) == 1 else None


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
        aliases = await svc.repo.aliases_map("vk") if is_admin else None
        trainings = await svc.repo.list_upcoming()
        if not trainings:
            await _send(user_id, "Ближайших тренировок нет.",
                        keyboard=_menu_kb(is_admin))
            return
        for tr in trainings:
            card = await views.training_card_plain(svc, tr, aliases)
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
        is_admin = admin_uid is not None and tenant.admin_vk_id == admin_uid
        aliases = await svc.repo.aliases_map("vk") if is_admin else None
        card = await views.training_card_plain(svc, training, aliases)
        full = await _is_full(svc, training)
    try:
        await _api().messages.edit(
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
        await _api().messages.edit(
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


def _bcast_confirm_kb() -> str:
    """Кнопки подтверждения рассылки."""
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    kb = Keyboard(inline=True)
    kb.add(Callback("✅ Отправить", payload={"a": "bcast_yes"}),
           color=KeyboardButtonColor.POSITIVE)
    kb.add(Callback("❌ Отмена", payload={"a": "bcast_no"}),
           color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


async def _start_bcast(user_id: int, group_id=None) -> None:
    """Начинает рассылку (только админ): просит ввести текст."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Рассылку может делать только тренер.")
            return
    _fsm_set(user_id, {"kind": "bcast", "gid": group_id})
    await _send(user_id, "📢 Введите текст рассылки для всех подписчиков:",
                keyboard=_cancel_kb())


async def _do_bcast(user_id: int, group_id=None) -> str:
    """Отправляет рассылку из сохранённого текста."""
    state = _fsm.get(user_id)
    if not state or "text" not in state:
        return "Нет текста для рассылки."
    text = state["text"]
    _fsm.pop(user_id, None)
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        res = await svc.broadcast(text)
    total = res.get("tg", 0) + res.get("vk", 0)
    return f"✅ Рассылка отправлена ({total} получателей)."


async def _start_create(user_id: int, group_id=None) -> None:
    """Начинает диалог создания (проверяет права админа)."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Создавать тренировки может только тренер.")
            return
    _fsm_set(user_id, {"step": "title", "data": {}, "gid": group_id, "manual": False})
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
        await _api().messages.edit(
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
    # редактирование шаблона: название и ручной ввод
    if state.get("kind") == "sched_edit":
        field = state["field"]
        gid = state["gid"]
        if not state.get("manual"):
            await _send(user_id, "Пожалуйста, выберите вариант кнопкой "
                        "или нажмите «✏️ Ввести вручную».")
            return True
        if field == "time":
            try:
                _dt.datetime.strptime(text, "%H:%M")
            except ValueError:
                await _send(user_id, "Формат: ЧЧ:ММ (напр. 19:30)",
                            keyboard=_cancel_kb()); return True
        elif field in ("duration", "price", "max", "ahead"):
            if not text.isdigit():
                await _send(user_id, "Введите число.", keyboard=_cancel_kb())
                return True
        res = await _sched_edit_apply(user_id, gid, text)
        await _send(user_id, res, keyboard=_menu_kb(True))
        return True

    # мастер расписания: название текстом + ручной ввод
    if state.get("kind") == "sched":
        step = state["step"]
        gid = state["gid"]
        data = state["data"]
        if step == "title":
            if not text:
                await _send(user_id, "Название пустое. Введите ещё раз:",
                            keyboard=_cancel_kb()); return True
            data["title"] = text[:250]
            state["step"] = "location"
            await _sched_ask(user_id, "location", gid)
            return True
        if not state.get("manual"):
            await _send(user_id, "Пожалуйста, выберите вариант кнопкой "
                        "или нажмите «✏️ Ввести вручную».")
            return True
        if step == "time":
            try:
                _dt.datetime.strptime(text, "%H:%M")
            except ValueError:
                await _send(user_id, "Формат: ЧЧ:ММ (напр. 19:30)",
                            keyboard=_cancel_kb()); return True
            data["time"] = text
        elif step == "location":
            data["location"] = text[:250]
        elif step in ("duration", "price", "max", "ahead"):
            if not text.isdigit():
                await _send(user_id, "Введите число.", keyboard=_cancel_kb())
                return True
            key = {"duration": "duration_min", "price": "price_minor",
                   "max": "max_participants", "ahead": "days_ahead"}[step]
            data[key] = int(text) * (100 if step == "price" else 1)
        await _sched_advance(user_id, gid)
        return True

    # ввод подписи участника (переименование)
    if state.get("kind") == "rename":
        target = state["target"]
        gid = state["gid"]
        _fsm.pop(user_id, None)
        res = await _rename_apply(user_id, target, text, gid)
        await _send(user_id, res, keyboard=_menu_kb(True))
        return True

    # ручной ввод нового значения при редактировании
    if state.get("kind") == "edit":
        if not state.get("manual"):
            await _send(user_id, "Выберите вариант кнопкой "
                        "или «✏️ Ввести вручную».")
            return True
        field = state["field"]
        gid = state["gid"]
        # валидация по типу поля
        if field == "date":
            try:
                _dt.datetime.strptime(text, "%d.%m.%Y")
            except ValueError:
                await _send(user_id, "Формат: ДД.ММ.ГГГГ", keyboard=_cancel_kb())
                return True
            res = await _edit_apply(user_id, gid,
                                    _dt.date(*map(int, reversed(text.split(".")))).isoformat())
        elif field == "time":
            try:
                _dt.datetime.strptime(text, "%H:%M")
            except ValueError:
                await _send(user_id, "Формат: ЧЧ:ММ", keyboard=_cancel_kb())
                return True
            res = await _edit_apply(user_id, gid, text)
        elif field in ("duration", "price", "max"):
            if not text.isdigit():
                await _send(user_id, "Введите число.", keyboard=_cancel_kb())
                return True
            res = await _edit_apply(user_id, gid, text)
        else:  # location
            res = await _edit_apply(user_id, gid, text)
        await _send(user_id, res, keyboard=_menu_kb(True))
        return True

    # мини-диалог рассылки: ввод текста → подтверждение
    if state.get("kind") == "bcast":
        if not text:
            await _send(user_id, "Текст пустой. Введите сообщение для рассылки:",
                        keyboard=_cancel_kb()); return True
        state["text"] = text[:2000]
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, state["gid"])
            subs = await BookingService(session, tenant.id).repo.get_subscribers()
        n = len(subs)
        await _send(user_id,
                    f"📢 Разослать это сообщение {n} подписчикам?\n\n«{text}»",
                    keyboard=_bcast_confirm_kb())
        return True

    if state.get("kind") == "guest":
        _fsm.pop(user_id, None)
        if not text:
            await _send(user_id, "Имя пустое.", keyboard=_menu_kb(True)); return True
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, state["gid"])
            svc = BookingService(session, tenant.id, tz=tenant.timezone)
            res = await svc.sign_up_guest(state["tid"], text[:250], added_by=user_id)
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
        data["title"] = text[:250]
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
        data["location"] = (text or "—")[:250]
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





# ─────────── Редактирование тренировки (кнопки, только админ) ───────────

def _edit_menu_kb(tid: int) -> str:
    """Меню выбора: что изменить в тренировке."""
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    kb = Keyboard(inline=True)
    kb.add(Callback("📅 Дату", payload={"a": "ed_f", "f": "date", "tid": tid}))
    kb.add(Callback("🕐 Время", payload={"a": "ed_f", "f": "time", "tid": tid}))
    kb.row()
    kb.add(Callback("📍 Место", payload={"a": "ed_f", "f": "location", "tid": tid}))
    kb.add(Callback("⏱ Длит.", payload={"a": "ed_f", "f": "duration", "tid": tid}))
    kb.row()
    kb.add(Callback("💰 Цену", payload={"a": "ed_f", "f": "price", "tid": tid}))
    kb.add(Callback("👥 Лимит", payload={"a": "ed_f", "f": "max", "tid": tid}))
    kb.row()
    kb.add(Callback("❌ Отмена", payload={"a": "ed_cancel"}),
           color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


async def _start_edit(user_id: int, tid: int, group_id=None) -> None:
    """Открывает меню редактирования тренировки (только админ)."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        tr = await svc.repo.get_training(tid)
        if not tr:
            await _send(user_id, "Тренировка не найдена."); return
        title = tr.title
    await _send(user_id, f"✏️ Что изменить в «{title}»?",
                keyboard=_edit_menu_kb(tid))


async def _edit_ask_value(user_id: int, tid: int, field: str, gid) -> None:
    """Показывает варианты нового значения для выбранного поля."""
    _fsm_set(user_id, {"kind": "edit", "tid": tid, "field": field,
                       "gid": gid, "manual": False})
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        if field == "date":
            await _send(user_id, "📅 Новая дата:",
                        keyboard=_kb_from(_date_options(svc.tz), per_row=2))
        elif field == "time":
            await _send(user_id, "🕐 Новое время:",
                        keyboard=_kb_from(_time_options(), per_row=4))
        elif field == "location":
            opts = await _location_options(svc)
            if opts:
                await _send(user_id, "📍 Новое место:",
                            keyboard=_kb_from(opts, per_row=1))
            else:
                _fsm[user_id]["manual"] = True
                await _send(user_id, "📍 Введите новое место:",
                            keyboard=_cancel_kb())
        elif field == "duration":
            await _send(user_id, "⏱ Новая длительность:",
                        keyboard=_kb_from(_duration_options(), per_row=4))
        elif field == "price":
            await _send(user_id, "💰 Новая цена:",
                        keyboard=_kb_from(_price_options(), per_row=3))
        elif field == "max":
            await _send(user_id, "👥 Новый лимит участников:",
                        keyboard=_kb_from(_max_options(), per_row=3))


async def _edit_apply(user_id: int, gid, raw_value) -> str:
    """Применяет новое значение к полю тренировки."""
    state = _fsm.get(user_id)
    if not state or state.get("kind") != "edit":
        return "Нет активного редактирования."
    field = state["field"]
    tid = state["tid"]
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        tr = await svc.repo.get_training(tid)
        if not tr:
            _fsm.pop(user_id, None)
            return "Тренировка не найдена."
        if field == "date":
            cur = svc.format_local(tr.start_at)
            time_part = cur.split(" ")[1]
            new = svc.parse_local(f"{_fmt_date(raw_value)} {time_part}")
            await svc.update_field(tid, "start_at", new)
            label = f"дата → {_fmt_date(raw_value)}"
        elif field == "time":
            cur = svc.format_local(tr.start_at)
            date_part = cur.split(" ")[0]
            new = svc.parse_local(f"{date_part} {raw_value}")
            await svc.update_field(tid, "start_at", new)
            label = f"время → {raw_value}"
        elif field == "location":
            await svc.update_field(tid, "location", raw_value)
            label = f"место → {raw_value}"
        elif field == "duration":
            await svc.update_field(tid, "duration_min", int(raw_value))
            label = f"длительность → {int(raw_value)} мин"
        elif field == "price":
            await svc.update_field(tid, "price_minor", int(raw_value) * 100)
            label = (f"цена → {int(raw_value)}₽" if int(raw_value)
                     else "цена → бесплатно")
        elif field == "max":
            await svc.update_field(tid, "max_participants", int(raw_value))
            label = f"лимит → {int(raw_value)}"
        else:
            _fsm.pop(user_id, None)
            return "Неизвестное поле."
        # уведомляем записанных об изменении (кроме мелочей вроде лимита)
        notified = 0
        readable = {
            "date": "📅 Новая дата",
            "time": "🕐 Новое время",
            "location": "📍 Новое место",
            "duration": "⏱ Новая длительность",
            "price": "💰 Новая цена",
            "max": "👥 Новый лимит",
        }.get(field, "Изменение")
        try:
            notified = await svc.notify_changed(tid, f"{readable}: {label.split('→ ')[-1]}")
        except Exception as e:
            logger.warning("VK: не удалось уведомить об изменении: %s", e)
    _fsm.pop(user_id, None)
    tail = f" ({notified} участникам отправлено уведомление)" if notified else ""
    return f"✅ Изменено: {label}{tail}"


async def _edit_callback(user_id: int, payload: dict, gid) -> str | None:
    """Обработка кнопок выбора нового значения при редактировании."""
    state = _fsm.get(user_id)
    if not state or state.get("kind") != "edit":
        return None
    a = payload.get("a")
    v = payload.get("v")
    if a == "cr_manual":
        state["manual"] = True
        prompts = {
            "date": "Введите дату: ДД.ММ.ГГГГ", "time": "Введите время: ЧЧ:ММ",
            "location": "Введите место:", "duration": "Введите минуты:",
            "price": "Введите цену в рублях:", "max": "Введите лимит:",
        }
        await _send(user_id, prompts.get(state["field"], "Введите значение:"),
                    keyboard=_cancel_kb())
        return None
    val = {"cr_date": v, "cr_time": v, "cr_loc": v, "cr_dur": v,
           "cr_price": v, "cr_max": v}.get(a)
    if val is None:
        return None
    return await _edit_apply(user_id, gid, val)


# ─────────── Переименование участников (приватные подписи) ───────────

async def _rename_pick(user_id: int, group_id=None) -> None:
    """Показывает участников всех тренировок для выбора и переименования."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        trainings = await svc.repo.list_upcoming()
        # собираем уникальных участников (vk) по всем предстоящим тренировкам
        seen = {}
        for tr in trainings:
            for s in await svc.repo.get_signups(tr.id, "active"):
                if getattr(s, "is_guest", False):
                    continue
                if s.platform == "vk" and s.user_id not in seen:
                    seen[s.user_id] = s.name
        aliases = await svc.repo.aliases_map("vk")
    if not seen:
        await _send(user_id, "Нет участников для переименования.",
                    keyboard=_menu_kb(True))
        return
    from vkbottle import Keyboard, Callback
    kb = Keyboard(inline=True)
    for uid, name in list(seen.items())[:8]:
        cur = aliases.get(uid)
        label = f"{cur or name}"[:28]
        kb.add(Callback(label, payload={"a": "rn_pick", "uid": uid}))
        kb.row()
    kb.add(Callback("❌ Закрыть", payload={"a": "rn_close"}))
    await _send(user_id, "✏️ Кого переименовать? (подпись видите только вы)",
                keyboard=kb.get_json())


async def _rename_start(user_id: int, target_uid: int, group_id=None) -> None:
    """Запрашивает новую подпись для выбранного участника."""
    _fsm_set(user_id, {"kind": "rename", "target": target_uid, "gid": group_id})
    await _send(user_id,
                "Введите подпись для участника (или «-» чтобы убрать):",
                keyboard=_cancel_kb())


async def _rename_apply(user_id: int, target_uid: int, alias: str,
                        group_id=None) -> str:
    """Сохраняет подпись участника (видна только тренеру)."""
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        value = None if alias.strip() in ("-", "") else alias.strip()[:100]
        display = await svc.repo.set_alias("vk", target_uid, value)
        await session.commit()
    if value:
        return f"✅ Подпись сохранена: {display}"
    return "✅ Подпись убрана."


# ─────────── Регулярное расписание (только админ) ───────────
_WD_FULL = ["Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье"]


async def _show_schedules(user_id: int, group_id=None) -> None:
    """Список регулярных расписаний: ✏️ изменить / 🗑 удалить / ➕ добавить."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        schedules = await svc.repo.list_schedules()
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    kb = Keyboard(inline=True)
    if schedules:
        lines = ["📆 Регулярное расписание:"]
        # лимит ВК: до 10 inline-кнопок — показываем кнопки максимум для 4
        for n, sch in enumerate(schedules[:4], 1):
            price = f", {sch.price_minor // 100}₽" if sch.price_minor else ""
            lines.append(f"{n}. {_WD_RU[sch.weekday]} {sch.time_str} — "
                         f"{sch.title} (макс {sch.max_participants}{price}, "
                         f"создаётся за {sch.days_ahead} дн.)")
            kb.add(Callback(f"✏️ {n}", payload={"a": "sch_edit", "sid": sch.id}))
            kb.add(Callback(f"🗑 {n}", payload={"a": "sch_del", "sid": sch.id}),
                   color=KeyboardButtonColor.NEGATIVE)
            kb.row()
        if len(schedules) > 4:
            lines.append(f"…и ещё {len(schedules) - 4} (удалите лишние, "
                         "чтобы управлять остальными)")
        lines.append("\nТренировка создаётся автоматически за указанное число "
                     "дней — в этот момент открывается запись и подписчики "
                     "получают уведомление.")
        text = "\n".join(lines)
    else:
        text = ("📆 Регулярного расписания пока нет.\n"
                "Добавьте шаблон — тренировки будут создаваться автоматически "
                "каждую неделю.")
    kb.add(Callback("➕ Добавить", payload={"a": "sch_add"}),
           color=KeyboardButtonColor.POSITIVE)
    await _send(user_id, text, keyboard=kb.get_json())


async def _sched_add_start(user_id: int, gid) -> None:
    """Мастер добавления расписания: сначала день недели."""
    _fsm_set(user_id, {"kind": "sched", "data": {}, "gid": gid,
                       "step": "wd", "manual": False})
    opts = [(_WD_FULL[i], {"a": "sch_wd", "v": i}) for i in range(7)]
    await _send(user_id, "📆 Какой день недели?",
                keyboard=_kb_from(opts, add_manual=False, per_row=2))


async def _sched_ask(user_id: int, step: str, gid) -> None:
    """Вопрос текущего шага мастера расписания."""
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        if step == "time":
            await _send(user_id, "🕐 Время занятия:",
                        keyboard=_kb_from(_time_options(), per_row=4))
        elif step == "title":
            await _send(user_id, "📝 Название тренировки (текстом):",
                        keyboard=_cancel_kb())
        elif step == "location":
            opts = await _location_options(svc)
            if opts:
                await _send(user_id, "📍 Место:",
                            keyboard=_kb_from(opts, per_row=1))
            else:
                _fsm[user_id]["manual"] = True
                await _send(user_id, "📍 Введите место:", keyboard=_cancel_kb())
        elif step == "duration":
            await _send(user_id, "⏱ Длительность:",
                        keyboard=_kb_from(_duration_options(), per_row=4))
        elif step == "price":
            await _send(user_id, "💰 Цена:",
                        keyboard=_kb_from(_price_options(), per_row=3))
        elif step == "max":
            await _send(user_id, "👥 Максимум участников:",
                        keyboard=_kb_from(_max_options(), per_row=3))
        elif step == "ahead":
            await _send(user_id, "📅 За сколько дней до занятия создавать "
                        "тренировку и открывать запись?",
                        keyboard=_kb_from(_ahead_options(), per_row=2))


def _ahead_options() -> list[tuple[str, dict]]:
    return [("За 1 день", {"a": "sch_da", "v": 1}),
            ("За 2 дня", {"a": "sch_da", "v": 2}),
            ("За 3 дня", {"a": "sch_da", "v": 3}),
            ("За 5 дней", {"a": "sch_da", "v": 5}),
            ("За неделю", {"a": "sch_da", "v": 7})]


_SCHED_ORDER = ["wd", "time", "title", "location", "duration",
                "price", "max", "ahead"]


async def _sched_advance(user_id: int, gid) -> None:
    state = _fsm.get(user_id)
    if not state:
        return
    cur = state["step"]
    nxt = _SCHED_ORDER[_SCHED_ORDER.index(cur) + 1] if cur in _SCHED_ORDER[:-1] else None
    if nxt is None:
        await _sched_save(user_id, gid)
        return
    state["step"] = nxt
    state["manual"] = False
    await _sched_ask(user_id, nxt, gid)


async def _sched_save(user_id: int, gid) -> None:
    state = _fsm.pop(user_id, None)
    if not state:
        return
    d = state["data"]
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        await svc.repo.add_schedule(
            weekday=d["weekday"], time_str=d["time"], title=d["title"],
            location=d.get("location", ""), duration_min=d["duration_min"],
            price_minor=d.get("price_minor", 0),
            max_participants=d["max_participants"],
            days_ahead=d.get("days_ahead", 3))
        await session.commit()
    await _send(user_id,
                f"✅ Расписание добавлено: {_WD_FULL[d['weekday']]} {d['time']} — "
                f"«{d['title']}».\nТренировка будет создаваться автоматически "
                f"за {d.get('days_ahead', 3)} дн. до занятия, подписчики получат уведомление.",
                keyboard=_menu_kb(True))


async def _sched_callback(user_id: int, payload: dict, gid,
                          peer_id=None, cmid=None) -> None:
    """Кнопки мастера расписания (день недели + общие cr_*)."""
    state = _fsm.get(user_id)
    if not state or state.get("kind") != "sched":
        return
    a = payload.get("a")
    v = payload.get("v")
    data = state["data"]
    if a == "cr_manual":
        state["manual"] = True
        prompts = {"time": "Введите время: ЧЧ:ММ", "location": "Введите место:",
                   "duration": "Введите минуты:", "price": "Введите цену в рублях:",
                   "max": "Введите максимум участников:"}
        await _strip_buttons(peer_id, cmid, "✏️ Ввод вручную…")
        await _send(user_id, prompts.get(state["step"], "Введите значение:"),
                    keyboard=_cancel_kb())
        return
    label = None
    if a == "sch_wd":
        data["weekday"] = int(v); label = f"✅ День: {_WD_FULL[int(v)]}"
    elif a == "cr_time":
        data["time"] = v; label = f"✅ Время: {v}"
    elif a == "cr_loc":
        data["location"] = v; label = f"✅ Место: {v}"
    elif a == "cr_dur":
        data["duration_min"] = int(v); label = f"✅ Длительность: {int(v)} мин"
    elif a == "cr_price":
        data["price_minor"] = int(v) * 100
        label = f"✅ Цена: {v}₽" if int(v) else "✅ Бесплатно"
    elif a == "cr_max":
        data["max_participants"] = int(v); label = f"✅ Максимум: {v}"
    elif a == "sch_da":
        data["days_ahead"] = int(v); label = f"✅ Создавать за {int(v)} дн."
    else:
        return
    if label:
        await _strip_buttons(peer_id, cmid, label)
    await _sched_advance(user_id, gid)


async def _sched_edit_menu(user_id: int, sid: int, gid) -> None:
    """Меню: что изменить в шаблоне расписания."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, gid):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        sch = await svc.repo.get_schedule(sid)
        if not sch:
            await _send(user_id, "Шаблон не найден."); return
        title = f"{_WD_RU[sch.weekday]} {sch.time_str} — {sch.title}"
    from vkbottle import Keyboard, Callback, KeyboardButtonColor
    kb = Keyboard(inline=True)
    kb.add(Callback("📆 День", payload={"a": "sch_ef", "f": "wd", "sid": sid}))
    kb.add(Callback("🕐 Время", payload={"a": "sch_ef", "f": "time", "sid": sid}))
    kb.row()
    kb.add(Callback("📝 Название", payload={"a": "sch_ef", "f": "title", "sid": sid}))
    kb.add(Callback("📍 Место", payload={"a": "sch_ef", "f": "location", "sid": sid}))
    kb.row()
    kb.add(Callback("⏱ Длит.", payload={"a": "sch_ef", "f": "duration", "sid": sid}))
    kb.add(Callback("💰 Цена", payload={"a": "sch_ef", "f": "price", "sid": sid}))
    kb.add(Callback("👥 Лимит", payload={"a": "sch_ef", "f": "max", "sid": sid}))
    kb.row()
    kb.add(Callback("📅 За сколько дней", payload={"a": "sch_ef", "f": "ahead", "sid": sid}))
    kb.row()
    kb.add(Callback("❌ Отмена", payload={"a": "ed_cancel"}),
           color=KeyboardButtonColor.NEGATIVE)
    await _send(user_id, f"✏️ Что изменить в шаблоне «{title}»?",
                keyboard=kb.get_json())


async def _sched_edit_ask(user_id: int, sid: int, field: str, gid) -> None:
    """Показывает варианты нового значения для поля шаблона."""
    _fsm_set(user_id, {"kind": "sched_edit", "sid": sid, "field": field,
                       "gid": gid, "manual": False})
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        if field == "wd":
            opts = [(_WD_FULL[i], {"a": "sch_wd", "v": i}) for i in range(7)]
            await _send(user_id, "📆 Новый день недели:",
                        keyboard=_kb_from(opts, add_manual=False, per_row=2))
        elif field == "time":
            await _send(user_id, "🕐 Новое время:",
                        keyboard=_kb_from(_time_options(), per_row=4))
        elif field == "title":
            _fsm[user_id]["manual"] = True
            await _send(user_id, "📝 Новое название (текстом):",
                        keyboard=_cancel_kb())
        elif field == "location":
            opts = await _location_options(svc)
            if opts:
                await _send(user_id, "📍 Новое место:",
                            keyboard=_kb_from(opts, per_row=1))
            else:
                _fsm[user_id]["manual"] = True
                await _send(user_id, "📍 Введите новое место:",
                            keyboard=_cancel_kb())
        elif field == "duration":
            await _send(user_id, "⏱ Новая длительность:",
                        keyboard=_kb_from(_duration_options(), per_row=4))
        elif field == "price":
            await _send(user_id, "💰 Новая цена:",
                        keyboard=_kb_from(_price_options(), per_row=3))
        elif field == "max":
            await _send(user_id, "👥 Новый лимит:",
                        keyboard=_kb_from(_max_options(), per_row=3))
        elif field == "ahead":
            await _send(user_id, "📅 За сколько дней создавать тренировку?",
                        keyboard=_kb_from(_ahead_options(), per_row=2))


async def _sched_edit_apply(user_id: int, gid, raw_value) -> str:
    """Применяет новое значение к полю шаблона расписания."""
    state = _fsm.pop(user_id, None)
    if not state or state.get("kind") != "sched_edit":
        return "Нет активного редактирования."
    field = state["field"]
    sid = state["sid"]
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        sch = await svc.repo.get_schedule(sid)
        if not sch:
            return "Шаблон не найден."
        if field == "wd":
            sch.weekday = int(raw_value)
            sch.last_date = ""   # чтобы создалось новое занятие по новому дню
            label = f"день → {_WD_FULL[int(raw_value)]}"
        elif field == "time":
            sch.time_str = raw_value
            sch.last_date = ""
            label = f"время → {raw_value}"
        elif field == "title":
            sch.title = raw_value[:250]
            label = f"название → {sch.title}"
        elif field == "location":
            sch.location = raw_value[:250]
            label = f"место → {sch.location}"
        elif field == "duration":
            sch.duration_min = int(raw_value)
            label = f"длительность → {int(raw_value)} мин"
        elif field == "price":
            sch.price_minor = int(raw_value) * 100
            label = (f"цена → {int(raw_value)}₽" if int(raw_value)
                     else "цена → бесплатно")
        elif field == "max":
            sch.max_participants = int(raw_value)
            label = f"лимит → {int(raw_value)}"
        elif field == "ahead":
            sch.days_ahead = int(raw_value)
            label = f"создавать за {int(raw_value)} дн."
        else:
            return "Неизвестное поле."
        await session.commit()
    note = (" Уже созданные тренировки не меняются — при необходимости "
            "отредактируйте или удалите их отдельно." if field in ("wd", "time")
            else "")
    return f"✅ Шаблон изменён: {label}.{note}"


async def _sched_edit_callback(user_id: int, payload: dict, gid,
                               peer_id=None, cmid=None) -> None:
    """Кнопки выбора нового значения при редактировании шаблона."""
    state = _fsm.get(user_id)
    if not state or state.get("kind") != "sched_edit":
        return
    a = payload.get("a")
    v = payload.get("v")
    if a == "cr_manual":
        state["manual"] = True
        prompts = {"time": "Введите время: ЧЧ:ММ", "location": "Введите место:",
                   "duration": "Введите минуты:", "price": "Введите цену в рублях:",
                   "max": "Введите лимит:", "ahead": "Введите число дней:"}
        await _strip_buttons(peer_id, cmid, "✏️ Ввод вручную…")
        await _send(user_id, prompts.get(state["field"], "Введите значение:"),
                    keyboard=_cancel_kb())
        return
    val = {"sch_wd": v, "cr_time": v, "cr_loc": v, "cr_dur": v,
           "cr_price": v, "cr_max": v, "sch_da": v}.get(a)
    if val is None:
        return
    res = await _sched_edit_apply(user_id, gid, val)
    await _strip_buttons(peer_id, cmid, res)
    await _send(user_id, res, keyboard=_menu_kb(True))


# ─────────── Настройка напоминания участникам (только админ) ───────────

async def _show_reminder(user_id: int, group_id=None) -> None:
    """Показывает текущее напоминание и варианты изменения."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, group_id)
        enabled = tenant.reminder_enabled
        minutes = tenant.reminder_minutes
    if not enabled:
        cur = "выключено"
    elif minutes % 60 == 0:
        cur = f"за {minutes // 60} ч до начала"
    else:
        cur = f"за {minutes} мин до начала"
    opts = [("Выключить", {"a": "rem_set", "v": 0}),
            ("За 30 мин", {"a": "rem_set", "v": 30}),
            ("За 1 час", {"a": "rem_set", "v": 60}),
            ("За 2 часа", {"a": "rem_set", "v": 120}),
            ("За 3 часа", {"a": "rem_set", "v": 180}),
            ("За сутки", {"a": "rem_set", "v": 1440})]
    await _send(user_id,
                f"⏰ Напоминание участникам «Скоро тренировка»: {cur}.\n"
                "Когда напоминать записанным?",
                keyboard=_kb_from(opts, add_manual=False, per_row=2))


async def _apply_reminder(user_id: int, minutes: int, group_id=None) -> str:
    """Сохраняет настройку напоминания клуба."""
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None or tenant.admin_vk_id != user_id:
            return "⛔ Только тренер."
        if minutes <= 0:
            tenant.reminder_enabled = False
            res = "✅ Напоминания выключены."
        else:
            tenant.reminder_enabled = True
            tenant.reminder_minutes = minutes
            human = (f"{minutes // 60} ч" if minutes % 60 == 0
                     else f"{minutes} мин")
            res = f"✅ Напоминание: за {human} до начала."
        await session.commit()
    return res


# ─────────── Рейтинг и статистика ───────────

async def _show_rating(user_id: int, group_id=None) -> None:
    """Топ посещаемости клуба — доступен всем участникам."""
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None:
            await _send(user_id, "Клуб не привязан."); return
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        rows = await svc.attendance_ranking()
        me = await svc.user_stats(PLATFORM, user_id)
        is_admin = tenant.admin_vk_id == user_id
    if not rows:
        await _send(user_id, "Пока нет данных о посещениях.",
                    keyboard=_menu_kb(is_admin))
        return
    text = views.ranking_text(rows)
    if me and me.get("attended"):
        text += f"\n\n👤 Вы: посещений {me.get('attended', 0)}"
    await _send(user_id, text, keyboard=_menu_kb(is_admin))


async def _show_stats_vk(user_id: int, group_id=None) -> None:
    """Статистика клуба для тренера + кнопки экспорта CSV."""
    if not features.statistics:
        await _send(user_id, "📊 Статистика доступна в версии Pro.")
        return
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        rows = await svc.attendance_ranking()
        debtors = await svc.list_debtors()
        trainings = await svc.repo.list_upcoming()
    lines = [views.ranking_text(rows) if rows else "Пока нет данных о посещениях."]
    if debtors:
        total = sum(d["debts"] for d in debtors)
        lines.append(f"\n💰 Должников: {len(debtors)} (долгов: {total})")
    # месячная сводка
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        summary = await svc.monthly_summary()
        past = await svc.repo.list_past(limit=8)
    if summary:
        lines.append("\n📅 По месяцам:")
        for r in summary:
            y, m = r["month"].split("-")
            lines.append(f"  {m}.{y}: тренировок {r['trainings']}, "
                         f"посещений {r['attended']}")
    if past:
        lines.append("\n📜 Прошедшие тренировки:")
        for t in past:
            when = svc.format_local(t.start_at)
            lines.append(f"  • {t.title} — {when}")
    from vkbottle import Keyboard, Callback
    kb = Keyboard(inline=True)
    for tr in trainings[:5]:
        kb.add(Callback(f"📄 CSV: {tr.title}"[:35],
                        payload={"a": "exp", "tid": tr.id}))
        kb.row()
    lines.append("\n📄 Выгрузка списка участников — кнопкой ниже.")
    await _send(user_id, "\n".join(lines),
                keyboard=kb.get_json() if trainings else None)


async def _export_csv_vk(user_id: int, tid: int, group_id=None) -> str:
    """Отправляет CSV со списком участников тренировки в личку ВК."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, group_id):
            return "⛔ Только тренер."
        tenant = await _resolve_tenant(session, group_id)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        csv_text = await svc.export_training_csv(tid)
        tr = await svc.repo.get_training(tid)
    if not csv_text or not tr:
        return "Нет данных для выгрузки."
    try:
        from vkbottle import DocMessagesUploader
        uploader = DocMessagesUploader(_api())
        doc = await uploader.upload(
            f"training_{tid}.csv", csv_text.encode("utf-8-sig"),
            peer_id=user_id)
        await _api().messages.send(peer_id=user_id, random_id=0,
                                     attachment=doc,
                                     message=f"📄 Список: {tr.title}")
        return "📄 Файл отправлен"
    except Exception as e:
        logger.warning("VK: не удалось отправить CSV: %s", e)
        await _send(user_id, f"📄 {tr.title}\n\n{csv_text[:3500]}")
        return "Отправлено текстом"




async def _pick_training(user_id: int, gid, action: str, title: str) -> None:
    """Инлайн-выбор тренировки для действия из меню (явка / гость)."""
    async with SessionLocal() as session:
        if not await _is_admin_vk(session, user_id, gid):
            await _send(user_id, "⛔ Только тренер."); return
        tenant = await _resolve_tenant(session, gid)
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        trainings = await svc.repo.list_upcoming()
    if not trainings:
        await _send(user_id, "Нет предстоящих тренировок.",
                    keyboard=_menu_kb(True))
        return
    from vkbottle import Keyboard, Callback
    kb = Keyboard(inline=True)
    for tr in trainings[:6]:
        when = tr.start_at.strftime("%d.%m")
        kb.add(Callback(f"{when} {tr.title}"[:35],
                        payload={"a": action, "tid": tr.id}))
        kb.row()
    await _send(user_id, title, keyboard=kb.get_json())


async def _handle_text(user_id: int, text: str, group_id=None) -> None:
    """Обработка текстовых команд."""
    raw = (text or "").strip()
    # если идёт пошаговое создание — направляем ввод туда (нужен оригинал)
    if user_id in _fsm:
        if await _fsm_process(user_id, raw):
            return
    text = raw.lower()
    if text in ("начать", "start"):
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, group_id)
            hello = (tenant.welcome_text or "").strip() if tenant else ""
        if hello:
            await _send(user_id, hello)
        try:
            async with SessionLocal() as _s2:
                _t2 = await _resolve_tenant(_s2, group_id)
                if _t2 is not None and _t2.admin_vk_id == user_id:
                    _ob = await views.onboarding_text(
                        BookingService(_s2, _t2.id))
                    if _ob:
                        await _send(user_id, _ob)
        except Exception:
            pass
        await _show_list(user_id, group_id)
    elif text in ("список", "тренировки", "🏸 тренировки"):
        await _show_list(user_id, group_id)
    elif text in ("мои записи", "📅 мои записи", "моя тренировка", "мои"):
        await _show_my(user_id, group_id)
    elif text in ("рассылка", "📢 рассылка"):
        await _start_bcast(user_id, group_id)
    elif text in ("имена", "✏️ имена", "переименовать"):
        await _rename_pick(user_id, group_id)
    elif text in ("расписание", "📆 расписание"):
        await _show_schedules(user_id, group_id)
    elif text in ("напоминание", "⏰ напоминание"):
        await _show_reminder(user_id, group_id)
    elif text == "демо":
        async with SessionLocal() as session:
            tenant = await _resolve_tenant(session, group_id)
            if tenant is None or tenant.admin_vk_id != user_id:
                return
            svc = BookingService(session, tenant.id, tz=tenant.timezone)
            ok = await svc.seed_demo()
        await _send(user_id, "✅ Демо-данные добавлены. Нажмите «🏸 Тренировки»."
                    if ok else "В клубе уже есть тренировки — демо только "
                    "для пустого клуба.", keyboard=_menu_kb(True))
    elif text in ("ещё", "⋯ ещё", "еще", "⋯ еще"):
        async with SessionLocal() as session:
            ok = await _is_admin_vk(session, user_id, group_id)
        if ok:
            await _send(user_id, "Дополнительно:",
                        keyboard=_menu_kb(True, more=True))
    elif text in ("назад", "⬅️ назад"):
        async with SessionLocal() as session:
            ok = await _is_admin_vk(session, user_id, group_id)
        await _send(user_id, "Главное меню:", keyboard=_menu_kb(ok))
    elif text in ("явки", "✅ явки", "явка"):
        await _pick_training(user_id, group_id, "att",
                             "✅ Явка: выберите тренировку")
    elif text in ("записать гостя", "👤 записать гостя", "гость"):
        await _pick_training(user_id, group_id, "guest",
                             "👤 Гость: выберите тренировку")
    elif text in ("рейтинг", "🏆 рейтинг", "топ"):
        await _show_rating(user_id, group_id)
    elif text in ("статистика", "📊 статистика"):
        await _show_stats_vk(user_id, group_id)
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
    # мультиклиент: публикуем через api бота нужного клуба
    _tok = _ctx_api.set(_api_by_tenant.get(tenant_id)
                        or (_bot.api if _bot else None))
    try:
        return await _publish_to_wall_impl(tenant_id, training_id)
    finally:
        _ctx_api.reset(_tok)


async def _publish_to_wall_impl(tenant_id: int, training_id: int) -> None:
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
        await _api().wall.post(
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
        groups = await _api().groups.get_by_id()
        # vkbottle может вернуть список или объект с .groups
        g0 = groups[0] if isinstance(groups, list) else groups.groups[0]
        _group_id = g0.id
        logger.info("VK: сообщество id=%s определено", _group_id)
    except Exception as e:
        logger.warning("VK: не удалось определить group_id: %s", e)

    def _attach(b, gid_default):
        """Вешает обработчики на бота; api бота кладётся в контекст события."""
        @b.on.message()
        async def _on_message(message: Message):
            token = _ctx_api.set(b.api)
            try:
                gid = getattr(message, "group_id", None) or gid_default
                await _handle_text(message.from_id, message.text or "", gid)
            except Exception as e:
                logger.warning("VK: ошибка обработки сообщения: %s", e)
            finally:
                _ctx_api.reset(token)

        @b.on.raw_event(GroupEventType.MESSAGE_EVENT, dataclass=dict)
        async def _on_callback(event: dict):
            token = _ctx_api.set(b.api)
            try:
                await _process_callback(event)
            except Exception as e:
                logger.warning("VK: ошибка обработки нажатия: %s", e)
            finally:
                _ctx_api.reset(token)

    async def _process_callback(event: dict):
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
        # SaaS: приостановленный клуб не обрабатывает нажатия
        try:
            from app.core.config import tenant_suspended
            async with SessionLocal() as _ss:
                _t = await _resolve_tenant(_ss, gid)
            if _t is not None and tenant_suspended(_t):
                await _send(user_id, "⏸ Работа клуба временно приостановлена. "
                            "Обратитесь к тренеру.")
                return
        except Exception:
            pass
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
        elif action == "refresh":                    # обновить карточку на месте
            await _edit_card(peer_id, cmid, tid, gid, admin_uid=user_id)
            snackbar = "🔄 Список обновлён"
        elif action == "my":
            await _show_my(user_id, gid)
        elif action == "profile":
            await _handle_text(user_id, "профиль", gid)
        elif action == "create":
            await _start_create(user_id, gid)
        elif action == "bcast":
            await _start_bcast(user_id, gid)
        elif action == "bcast_yes":
            snackbar = await _do_bcast(user_id, gid)
            if cmid:
                try:
                    await _api().messages.edit(
                        peer_id=peer_id, conversation_message_id=cmid,
                        message=snackbar, keyboard="")
                except Exception:
                    await _send(user_id, snackbar, keyboard=_menu_kb(True))
        elif action == "bcast_no":
            _fsm.pop(user_id, None)
            snackbar = "Рассылка отменена"
            if cmid:
                try:
                    await _api().messages.edit(
                        peer_id=peer_id, conversation_message_id=cmid,
                        message="❌ Рассылка отменена.", keyboard="")
                except Exception:
                    pass
        elif action == "create_cancel":
            _fsm.pop(user_id, None)
            await _send(user_id, "Создание отменено.", keyboard=_menu_kb(True))
        elif action in ("cr_date", "cr_time", "cr_loc", "cr_dur",
                        "cr_price", "cr_max", "cr_manual"):
            st = _fsm.get(user_id)
            kind = st.get("kind") if st else None
            if kind == "edit":
                res = await _edit_callback(user_id, payload, gid)
                if res:
                    snackbar = res
                    await _strip_buttons(peer_id, cmid, res)
            elif kind == "sched":
                await _sched_callback(user_id, payload, gid, peer_id, cmid)
            elif kind == "sched_edit":
                await _sched_edit_callback(user_id, payload, gid, peer_id, cmid)
            else:
                await _cr_callback(user_id, payload, gid, peer_id, cmid)
        elif action == "sched":                      # список расписаний
            await _show_schedules(user_id, gid)
        elif action == "rem":                        # настройка напоминания
            await _show_reminder(user_id, gid)
        elif action == "rating":                     # топ посещаемости
            await _show_rating(user_id, gid)
        elif action == "menu_more":                  # второй экран меню
            await _send(user_id, "Дополнительно:",
                        keyboard=_menu_kb(True, more=True))
        elif action == "menu_back":                  # главное меню
            await _send(user_id, "Главное меню:", keyboard=_menu_kb(True))
        elif action == "att_pick":                   # явка: выбрать тренировку
            await _pick_training(user_id, gid, "att",
                                 "✅ Явка: выберите тренировку")
        elif action == "guest_pick":                 # гость: выбрать тренировку
            await _pick_training(user_id, gid, "guest",
                                 "👤 Гость: выберите тренировку")
        elif action == "stats":                      # статистика тренера
            await _show_stats_vk(user_id, gid)
        elif action == "exp":                        # экспорт CSV
            snackbar = await _export_csv_vk(user_id, tid, gid)
        elif action == "rem_set":
            snackbar = await _apply_reminder(user_id, int(payload.get("v", 0)), gid)
            await _strip_buttons(peer_id, cmid, snackbar)
        elif action == "cr_manual" and (_fsm.get(user_id) or {}).get("kind") == "rem":
            pass  # обработано выше в общем cr_ роутинге
        elif action == "sch_add":                    # добавить шаблон
            await _sched_add_start(user_id, gid)
        elif action in ("sch_wd", "sch_da"):          # день недели / за сколько дней
            st = _fsm.get(user_id)
            if st and st.get("kind") == "sched_edit":
                await _sched_edit_callback(user_id, payload, gid, peer_id, cmid)
            else:
                await _sched_callback(user_id, payload, gid, peer_id, cmid)
        elif action == "sch_edit":                    # меню редактирования шаблона
            await _sched_edit_menu(user_id, payload.get("sid"), gid)
        elif action == "sch_ef":                      # выбрано поле шаблона
            await _strip_buttons(peer_id, cmid, "✏️ Выбор нового значения…")
            await _sched_edit_ask(user_id, payload.get("sid"),
                                  payload.get("f"), gid)
        elif action == "sch_del":                    # удалить шаблон
            async with SessionLocal() as session:
                tenant = await _resolve_tenant(session, gid)
                if tenant is None or tenant.admin_vk_id != user_id:
                    snackbar = "⛔ Только тренер."
                else:
                    svc = BookingService(session, tenant.id, tz=tenant.timezone)
                    ok = await svc.repo.delete_schedule(payload.get("sid"))
                    await session.commit()
                    snackbar = "🗑 Удалено" if ok else "Не найдено"
            await _strip_buttons(peer_id, cmid, "🗑 Шаблон удалён." if
                                 snackbar.startswith("🗑") else snackbar)
        elif action == "rep":                        # повтор тренировки +7 дней
            async with SessionLocal() as session:
                tenant = await _resolve_tenant(session, gid)
                if tenant is None or tenant.admin_vk_id != user_id:
                    snackbar = "⛔ Только тренер."
                else:
                    svc = BookingService(session, tenant.id, tz=tenant.timezone)
                    new_t = await svc.repeat_training(tid, days_ahead=7)
                    if not new_t:
                        snackbar = "Тренировка не найдена."
                    else:
                        card = await views.training_card_plain(svc, new_t)
                        new_tid = new_t.id
                        snackbar = "🔁 Копия на +7 дней создана"
            if snackbar.startswith("🔁"):
                await _send(user_id, "🔁 Повтор тренировки:\n\n" + card,
                            keyboard=_kb(new_tid, False, True))
        elif action == "edit":                      # открыть меню редактирования
            await _start_edit(user_id, tid, gid)
        elif action == "names":                      # список для переименования
            await _rename_pick(user_id, gid)
        elif action == "rn_pick":                    # выбран участник
            await _strip_buttons(peer_id, cmid, "✏️ Ввод подписи…")
            await _rename_start(user_id, payload.get("uid"), gid)
        elif action == "rn_close":
            await _strip_buttons(peer_id, cmid, "Закрыто.")
        elif action == "ed_f":                       # выбрано поле для изменения
            await _strip_buttons(peer_id, cmid, "✏️ Выбор нового значения…")
            await _edit_ask_value(user_id, tid, payload.get("f"), gid)
        elif action == "ed_cancel":
            _fsm.pop(user_id, None)
            snackbar = "Редактирование отменено"
            await _strip_buttons(peer_id, cmid, "❌ Редактирование отменено.")
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
                    _fsm_set(user_id, {"kind": "guest", "tid": tid, "gid": gid})
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
                    await _api().messages.edit(
                        peer_id=peer_id, conversation_message_id=cmid,
                        message="⏳ Отменяю тренировку…", keyboard="")
                except Exception:
                    pass
            result = await _delete_training(user_id, tid, gid)
            snackbar = result
            # переписываем сообщение на итог (без кнопок) + отдельное уведомление
            if cmid:
                try:
                    await _api().messages.edit(
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
                    await _api().messages.edit(
                        peer_id=peer_id, conversation_message_id=cmid,
                        message="↩️ Отмена тренировки прервана.", keyboard="")
                except Exception:
                    pass

        # ответ на нажатие (всплывающее уведомление) + обновляем карточку
        try:
            await _api().messages.send_message_event_answer(
                event_id=event_id, user_id=user_id, peer_id=peer_id,
                event_data=json.dumps({"type": "show_snackbar",
                                       "text": snackbar[:90]}))
        except Exception as e:
            logger.warning("VK event answer error: %s", e)

    _attach(_bot, _group_id)

    # мультиклиент: поднимаем VK-ботов клубов с собственными токенами
    try:
        await _load_client_bots(_attach)
    except Exception as e:
        logger.warning("VK: не удалось прочитать клиентские токены: %s", e)
    globals()["_attach_fn"] = _attach   # для hot-reload

    tasks.register_sender(PLATFORM, _send)
    logger.info("VK готов (кнопки + Long Poll).")


async def _load_client_bots(attach) -> None:
    """(Пере)читывает клиентских VK-ботов из базы; поднимает новых,
    гасит убранных. Задачи поллинга создаются, если поллинг уже запущен."""
    from vkbottle.bot import Bot
    from sqlalchemy import select
    from app.models.entities import Tenant
    async with SessionLocal() as _s:
        tenants = list((await _s.execute(
            select(Tenant).where(Tenant.vk_token.is_not(None)))).scalars())
    fresh: dict[int, str] = {}
    for t in tenants:
        tok = (t.vk_token or "").strip()
        if tok and tok != settings.vk_token:
            fresh[t.id] = tok
    # гасим убранных и сменивших токен (сменившие поднимутся заново ниже)
    for tid in list(_client_bots.keys()):
        if fresh.get(tid) != _client_tokens.get(tid):
            task = _client_tasks.pop(tid, None)
            if task:
                task.cancel()
            _client_bots.pop(tid, None)
            _api_by_tenant.pop(tid, None)
            _client_tokens.pop(tid, None)
            logger.info("VK: клиентский бот клуба id=%s остановлен "
                        "(токен изменён или удалён)", tid)
    # поднимаем новых
    for tid, tok in fresh.items():
        if tid in _client_bots:
            continue
        try:
            cb = Bot(token=tok)
            groups = await cb.api.groups.get_by_id()
            g0 = groups[0] if isinstance(groups, list) else groups.groups[0]
            attach(cb, g0.id)
            _client_bots[tid] = cb
            _api_by_tenant[tid] = cb.api
            _client_tokens[tid] = tok
            if _vk_polling_active:
                _client_tasks[tid] = _spawn_client_poll(tid, cb)
            logger.info("VK: клиентский бот клуба id=%s (группа %s) поднят",
                        tid, g0.id)
        except Exception as e:
            logger.warning("VK: клиентский бот клуба id=%s не поднялся: %s",
                           tid, e)


async def reload_client_bots() -> None:
    """Мультиклиент: применяет VK-токены из базы без рестарта сервиса."""
    attach = globals().get("_attach_fn")
    if not _enabled or attach is None:
        return
    await _load_client_bots(attach)
    logger.info("VK: клиентские боты перечитаны (%d)", len(_client_bots))


async def _poll_forever(bot) -> None:
    """
    Безопасный аналог bot.run_polling() из vkbottle для вызова из УЖЕ
    работающего event loop (мы всегда внутри loop'а FastAPI/uvicorn).

    Проблема оригинального bot.run_polling(): он проверяет свой внутренний
    флаг loop_wrapper.is_running (не сам event loop!) — при первом вызове
    флаг всегда False, поэтому метод пытается САМ запустить/остановить
    event loop через синхронный LoopWrapper.run(). Тот, в свою очередь,
    видит, что реальный loop уже работает, и падает:
        RuntimeError: LoopWrapper.run() cannot be called from a running
        event loop. Use 'await bot.run_polling()' instead.
    Падение происходит ПОСЛЕ того, как bot.loop_wrapper.add_task(...) уже
    успел запланировать внутренний поллинг-таск на реальном loop'е (у него
    свой, отдельный от LoopWrapper, безопасный путь для уже запущенного
    loop'а) — то есть даже неудачный вызов оставляет позади «осиротевший»
    поллинг-таск, который никто не отслеживает и не отменяет. Раньше это
    исключение просто терялось молча (task exception was never retrieved),
    поэтому проблема была незаметна: спавнился ровно один осиротевший
    таск при старте и молча работал. После того как run_polling() стал
    попадать под supervise() (авто-ретрай), КАЖДЫЙ повторный вызов при
    падении добавлял ЕЩЁ один осиротевший поллинг-таск поверх предыдущих —
    отсюда дублирующиеся ответы (несколько параллельных Long Poll на один
    и тот же бот).

    Решение: не используем bot.run_polling()/LoopWrapper вообще, а
    напрямую воспроизводим то же самое (низкоуровневый bot.polling.listen()
    + диспетчеризация через bot.router.route — именно это делает
    run_polling() внутри), но без обращения к LoopWrapper. Так ошибка
    вообще не возникает, а не просто прячется, и повторные попытки
    supervise() безопасны и идемпотентны (каждая создаёт ровно один новый
    поллинг, не оставляя дублей от прежних попыток).
    """
    import asyncio as _aio
    polling = bot.polling
    async for event in polling.listen():
        for update in event.get("updates", []):
            _aio.create_task(bot.router.route(update, polling.api))


def _spawn_client_poll(tid: int, bot):
    """Запускает поллинг клиентского бота под супервизором: если Long Poll
    этого клуба упадёт с исключением, перезапустится сам (с бэкоффом),
    не утаскивая за собой ни платформенного бота, ни остальных клиентов."""
    import asyncio as _aio
    from functools import partial
    from app.services import tasks as _tasks
    return _aio.create_task(
        _tasks.supervise(f"VK-поллинг клиента id={tid}", partial(_poll_forever, bot)))


async def run_polling() -> None:
    global _vk_polling_active
    if _bot:
        _vk_polling_active = True
        n = 1 + len(_client_bots)
        logger.info("VK: запускаю Long Poll (ботов: %d)…", n)
        for tid, b in _client_bots.items():
            _client_tasks[tid] = _spawn_client_poll(tid, b)
        try:
            await _poll_forever(_bot)
        finally:
            _vk_polling_active = False
            for t in _client_tasks.values():
                t.cancel()


async def feed_callback_event(body: dict) -> None:
    """Обработка события VK Callback API (webhook)."""
    if not _enabled:
        return
    t = body.get("type")
    if t == "message_new":
        obj = body.get("object", {}).get("message", {})
        await _handle_text(obj.get("from_id"), obj.get("text") or "",
                           body.get("group_id"))
