"""
Telegram-бот (aiogram 3.x), мультитенантный, полный функционал.
Тенант — по chat_id (группа клуба) или по админу в личке. Каждый хендлер
открывает свою async-сессию и работает через BookingService с нужным
tenant_id — клубы изолированы.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message,
    ReplyKeyboardMarkup, Update,
)

from app.bots import views
from app.bots.user_info import fetch_tg_photo_url, profile_link
from app.core.config import settings
from app.core.features import features
from app.db.engine import SessionLocal
from app.repositories.repo import GlobalRepository
from app.services import charts, tasks
from app.services.booking import BookingService

logger = logging.getLogger("tg")
PLATFORM = "tg"

_bot: Bot | None = None
_dp: Dispatcher | None = None
router = Router()


async def _resolve_tenant(session, chat_id: int, user_id: int):
    g = GlobalRepository(session)
    tenant = await g.get_tenant_by_tg_chat(chat_id)
    if tenant is None:
        for t in await g.list_tenants():
            if t.admin_tg_id == user_id:
                tenant = t
                break
    if tenant is None:
        return None, False
    return tenant.id, (tenant.admin_tg_id == user_id)


def _name(x) -> str:
    u = x.from_user
    return u.full_name or (u.username or f"id{u.id}")


def _username(x) -> str | None:
    return x.from_user.username  # без @ (может быть None)


# Тексты кнопок постоянного меню (внизу экрана)
BTN_LIST = "🏸 Тренировки"
BTN_MY = "📅 Моя тренировка"
BTN_PROFILE = "👤 Профиль"
BTN_STATS = "📊 Статистика"
BTN_NEW = "➕ Создать"
BTN_ATTEND = "✅ Явка/оплата"
BTN_GUESTS = "👥 Гости"
BTN_DRAFTS = "📋 Черновики"
BTN_BROADCAST = "📢 Рассылка"
BTN_NAMES = "✏️ Имена"


def _menu(is_admin: bool) -> ReplyKeyboardMarkup:
    """Постоянное меню внизу экрана. У админа — расширенное."""
    rows = [[KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_MY)],
            [KeyboardButton(text=BTN_PROFILE)]]
    if features.statistics:
        rows[1].append(KeyboardButton(text=BTN_STATS))
    if is_admin:
        rows.append([KeyboardButton(text=BTN_NEW),
                     KeyboardButton(text=BTN_ATTEND)])
        rows.append([KeyboardButton(text=BTN_GUESTS),
                     KeyboardButton(text=BTN_DRAFTS)])
        rows.append([KeyboardButton(text=BTN_NAMES),
                     KeyboardButton(text=BTN_BROADCAST)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               input_field_placeholder="Выберите действие")


async def _upsert_user(svc: BookingService, user) -> None:
    """
    Сохраняет имя и username участника. Аватар подтягивается фоново —
    не блокирует запись, но к следующему открытию списка уже будет.
    """
    uid = user.id
    name = user.full_name or (user.username or f"id{uid}")
    uname = user.username
    tenant_id = svc.tenant_id  # сохраняем заранее, не держим ссылку на svc

    # сохраняем сразу с тем, что есть
    await svc.repo.upsert_subscriber(PLATFORM, uid, name, username=uname)
    await svc.session.commit()

    # аватар запрашиваем фоново — не задерживаем ответ пользователю
    if _bot:
        async def _bg(tid: int = tenant_id) -> None:
            try:
                photo = await fetch_tg_photo_url(_bot, uid)
                if not photo:
                    return
                async with SessionLocal() as s2:
                    svc2 = BookingService(s2, tid)
                    await svc2.repo.upsert_subscriber(
                        PLATFORM, uid, name, username=uname, photo_url=photo)
                    await s2.commit()
            except Exception as e:
                logger.debug("Фоновое обновление аватара не удалось: %s", e)
        asyncio.create_task(_bg())


def _kb(tid: int, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="✅ Записаться", callback_data=f"su:{tid}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"cx:{tid}"),
    ], [
        InlineKeyboardButton(text="👤 Записать гостя", callback_data=f"gu:{tid}"),
    ]]
    if is_admin:
        rows.append([
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ed:{tid}"),
            InlineKeyboardButton(text="🔁 Повторить", callback_data=f"rep:{tid}"),
        ])
        rows.append([
            InlineKeyboardButton(text="🗑 Отменить тренировку",
                                 callback_data=f"trcx:{tid}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class NewTraining(StatesGroup):
    title = State()
    date = State()          # выбор даты кнопками
    date_manual = State()   # ручной ввод даты
    time = State()          # выбор времени кнопками
    time_manual = State()   # ручной ввод времени
    location = State()      # выбор места кнопками
    location_manual = State()
    duration = State()
    duration_manual = State()
    maxp = State()
    maxp_manual = State()
    price = State()
    price_manual = State()
    pubmode = State()
    publish_at = State()


class SetMax(StatesGroup):
    value = State()


class Broadcast(StatesGroup):
    text = State()


class GuestSignup(StatesGroup):
    name = State()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Этот чат не привязан к клубу. Обратитесь к администратору платформы.")
            return
        # обновление профиля не должно мешать приветствию
        try:
            svc = BookingService(session, tid)
            await _upsert_user(svc, message.from_user)
            await svc.repo.set_subscription(PLATFORM, message.from_user.id, True)
            await session.commit()
        except Exception as e:
            logger.warning("Не удалось обновить профиль при /start: %s", e)
    text = (
        "🏸 <b>Добро пожаловать!</b>\n\n"
        "Это бот для записи на тренировки. Через меню внизу можно "
        "посмотреть тренировки, записаться и увидеть свою статистику.\n\n"
        "👇 Используйте кнопки меню под полем ввода."
    )
    if is_admin:
        text += ("\n\n🛠 <b>Вы администратор клуба.</b>\n"
                 "Вам доступны кнопки создания тренировок, отметки явки, "
                 "подтверждения гостей, черновиков и рассылки.")
    await message.answer(text, reply_markup=_menu(is_admin), parse_mode="HTML")


@router.message(F.text == BTN_LIST)
async def btn_list(message: Message) -> None:
    await cmd_list(message)


@router.message(F.text == BTN_MY)
async def btn_my(message: Message) -> None:
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Чат не привязан к клубу."); return
        svc = BookingService(session, tid)
        t = await svc.next_training_for_user(PLATFORM, message.from_user.id)
        if not t:
            await message.answer("📭 Вы не записаны ни на одну предстоящую тренировку.\n"
                                 "Нажмите «🏸 Тренировки», чтобы записаться."); return
        card = await views.training_card(svc, t)
    await message.answer("📅 <b>Ваша ближайшая тренировка:</b>\n\n" + card,
                         reply_markup=_kb(t.id, is_admin), parse_mode="HTML")


@router.message(F.text == BTN_PROFILE)
async def btn_profile(message: Message) -> None:
    await cmd_profile(message)


@router.message(F.text == BTN_STATS)
async def btn_stats(message: Message) -> None:
    await cmd_stats(message)


@router.message(F.text == BTN_NEW)
async def btn_new(message: Message, state: FSMContext) -> None:
    await cmd_new(message, state)


@router.message(F.text == BTN_ATTEND)
async def btn_attend(message: Message) -> None:
    await cmd_attend(message)


@router.message(F.text == BTN_GUESTS)
async def btn_guests(message: Message) -> None:
    await cmd_guests(message)


@router.message(F.text == BTN_DRAFTS)
async def btn_drafts(message: Message) -> None:
    await cmd_drafts(message)


@router.message(F.text == BTN_BROADCAST)
async def btn_broadcast(message: Message, state: FSMContext) -> None:
    await cmd_broadcast(message, state)


class RenameParticipant(StatesGroup):
    value = State()


@router.message(F.text == BTN_NAMES)
async def btn_names(message: Message) -> None:
    tid = await _admin_guard(message)
    if tid is None:
        return
    async with SessionLocal() as session:
        svc = BookingService(session, tid)
        people = await svc.repo.list_participants()
    if not people:
        await message.answer("Пока нет known участников. Они появятся здесь "
                             "после первой записи на тренировку."); return
    rows = []
    for p in people:
        shown = p.alias or p.name or f"id{p.user_id}"
        mark = "✏️ " if p.alias else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{shown}", callback_data=f"rn:{p.platform}:{p.user_id}")])
    await message.answer(
        "👥 <b>Участники клуба</b>\n"
        "Нажмите, чтобы задать свою подпись (как в телефонной книжке). "
        "✏️ — уже переименованные.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML")


@router.callback_query(F.data.startswith("rn:"))
async def cb_rename(query: CallbackQuery, state: FSMContext) -> None:
    _, platform, uid = query.data.split(":")
    async with SessionLocal() as session:
        tid = await _is_admin_cb(session, query)
        if tid is None:
            return
    await state.update_data(tenant_id=tid, platform=platform, user_id=int(uid))
    await state.set_state(RenameParticipant.value)
    await query.answer()
    await query.message.answer(
        "Введите подпись для участника (например «Вася вторник»).\n"
        "Отправьте «-» чтобы вернуть имя из Telegram.")


@router.message(RenameParticipant.value)
async def rename_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    alias = message.text.strip()
    if alias == "-":
        alias = None
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        display = await svc.repo.set_alias(data["platform"], data["user_id"], alias)
        await session.commit()
    await state.clear()
    if alias:
        await message.answer(f"✅ Участник теперь отображается как «{display}».")
    else:
        await message.answer(f"✅ Возвращено имя из Telegram: «{display}».")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Чат не привязан к клубу."); return
        svc = BookingService(session, tid)
        trainings = await svc.repo.list_upcoming()
        if not trainings:
            await message.answer("Ближайших тренировок нет."); return
        for t in trainings:
            await message.answer(await views.training_card(svc, t),
                                 reply_markup=_kb(t.id, is_admin),
                                 parse_mode="HTML")


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    async with SessionLocal() as session:
        tid, _ = await _resolve_tenant(session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Чат не привязан к клубу."); return
        svc = BookingService(session, tid)
        stats = await svc.user_stats(PLATFORM, message.from_user.id)
    await message.answer(views.profile_card(_name(message), stats))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not features.statistics:
        await message.answer("📊 Статистика и графики доступны в версии Pro."); return
    async with SessionLocal() as session:
        tid, _ = await _resolve_tenant(session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Чат не привязан к клубу."); return
        svc = BookingService(session, tid)
        rows = await svc.attendance_ranking()
    if not rows:
        await message.answer("Пока нет данных о посещениях."); return
    await message.answer(views.ranking_text(rows))
    png = charts.attendance_chart_png(rows)
    if png:
        await message.answer_photo(BufferedInputFile(png, "attendance.png"), caption="Посещаемость")


@router.callback_query(F.data.startswith("su:"))
async def cb_signup(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None:
            await query.answer("Чат не привязан к клубу.", show_alert=True); return
        svc = BookingService(session, tid)
        await _upsert_user(svc, query.from_user)
        res = await svc.sign_up(train_id, PLATFORM, query.from_user.id,
                                _name(query), username=_username(query))
        training = await svc.repo.get_training(train_id)
        new_card = await views.training_card(svc, training) if training else None
    await query.answer(views.signup_result(res, training.title if training else ""), show_alert=True)
    await _refresh_card(query, train_id, new_card, is_admin)
    await _refresh_group_card(tid, train_id)


async def _refresh_card(query, train_id: int, card: str | None,
                        is_admin: bool = False, prefix: str = "") -> None:
    """
    Перерисовывает карточку тренировки в том сообщении, где нажали кнопку —
    чтобы счётчик мест и список записавшихся обновлялись в реальном времени.
    Молча игнорирует ошибку 'message is not modified' и прочие.
    """
    if not card:
        return
    try:
        await query.message.edit_text(
            (prefix + card) if prefix else card,
            reply_markup=_kb(train_id, is_admin), parse_mode="HTML")
    except Exception:
        pass  # текст не изменился или сообщение недоступно — не критично


@router.callback_query(F.data.startswith("cx:"))
async def cb_cancel(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None:
            await query.answer("Чат не привязан к клубу.", show_alert=True); return
        g = GlobalRepository(session)
        tenant = await g.get_tenant(tid)
        lock = tenant.cancel_lock_minutes if tenant else 0
        svc = BookingService(session, tid)
        res = await svc.cancel_signup(train_id, PLATFORM, query.from_user.id,
                                      lock_minutes=lock)
        training = await svc.repo.get_training(train_id)
        new_card = await views.training_card(svc, training) if training else None
    if res.get("locked"):
        await query.answer(
            f"Отмена закрыта: до тренировки меньше {res['lock_minutes']} мин. "
            f"Свяжитесь с тренером.", show_alert=True)
        return
    await query.answer("Запись отменена." if res["cancelled"] else "Вы не были записаны.", show_alert=True)
    if res["cancelled"]:
        await _refresh_card(query, train_id, new_card, is_admin)
        await _refresh_group_card(tid, train_id)


# ---------- Управление тренировкой (админ) ----------

class EditTraining(StatesGroup):
    field = State()
    value = State()


async def _is_admin_cb(session, query) -> int | None:
    tid, is_admin = await _resolve_tenant(session, query.message.chat.id,
                                          query.from_user.id)
    if tid is None or not is_admin:
        await query.answer("Только для администратора.", show_alert=True)
        return None
    return tid


@router.callback_query(F.data.startswith("rep:"))
async def cb_repeat(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid = await _is_admin_cb(session, query)
        if tid is None:
            return
        svc = BookingService(session, tid)
        new_t = await svc.repeat_training(train_id, days_ahead=7)
        if not new_t:
            await query.answer("Не найдено", show_alert=True); return
        card = await views.training_card(svc, new_t)
    await query.answer("Создана копия на +7 дней ✅", show_alert=True)
    await query.message.answer("🔁 <b>Повтор тренировки:</b>\n\n" + card,
                               reply_markup=_kb(new_t.id, True), parse_mode="HTML")


@router.callback_query(F.data.startswith("trcx:"))
async def cb_cancel_training(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    # подтверждение
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, отменить", callback_data=f"trcxyes:{train_id}"),
        InlineKeyboardButton(text="Нет", callback_data="trcxno"),
    ]])
    await query.answer()
    await query.message.answer("⚠️ Отменить тренировку? Все записанные получат "
                               "уведомление.", reply_markup=kb)


@router.callback_query(F.data == "trcxno")
async def cb_cancel_training_no(query: CallbackQuery) -> None:
    await query.answer("Отмена отменена 🙂")
    await query.message.edit_text("Отмена тренировки прервана.")


@router.callback_query(F.data.startswith("trcxyes:"))
async def cb_cancel_training_yes(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid = await _is_admin_cb(session, query)
        if tid is None:
            return
        svc = BookingService(session, tid)
        training = await svc.repo.get_training(train_id)
        title = training.title if training else ""
        when = svc.format_local(training.start_at) if training else ""
        await svc.cancel_training(train_id)
        g = GlobalRepository(session)
        tenant = await g.get_tenant(tid)
        group_chat = tenant.tg_chat_id if tenant else None
    await query.answer("Тренировка отменена, все уведомлены.", show_alert=True)
    await query.message.edit_text("🗑 Тренировка отменена. Участники получили уведомление.")
    # уведомление в группу клуба
    if _bot and group_chat and group_chat != -100:
        try:
            await _bot.send_message(
                group_chat,
                f"🚫 <b>Тренировка отменена</b>\n{title} — {when}",
                parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось уведомить группу об отмене: %s", e)


@router.callback_query(F.data.startswith("ed:"))
async def cb_edit(query: CallbackQuery, state: FSMContext) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid = await _is_admin_cb(session, query)
        if tid is None:
            return
    await state.update_data(tenant_id=tid, train_id=train_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 Время", callback_data="edf:time"),
         InlineKeyboardButton(text="📍 Место", callback_data="edf:loc")],
        [InlineKeyboardButton(text="👥 Лимит", callback_data="edf:max"),
         InlineKeyboardButton(text="⏱ Длительность", callback_data="edf:dur")],
    ])
    await query.answer()
    await query.message.answer("Что изменить?", reply_markup=kb)


@router.callback_query(F.data.startswith("edf:"))
async def cb_edit_field(query: CallbackQuery, state: FSMContext) -> None:
    field = query.data.split(":")[1]
    await state.update_data(edit_field=field)
    await state.set_state(EditTraining.value)
    prompts = {
        "time": "Новые дата и время: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 20.07.2026 19:00)",
        "loc": "Новое место:",
        "max": "Новый лимит участников (число):",
        "dur": "Новая длительность в минутах (число):",
    }
    await query.answer()
    await query.message.answer(prompts[field])


@router.message(EditTraining.value)
async def edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data["edit_field"]
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        tid = data["train_id"]
        if field == "time":
            parsed = svc.parse_local(message.text)
            if not parsed:
                await message.answer("Неверный формат. Пример: 20.07.2026 19:00"); return
            await svc.update_field(tid, "start_at", parsed)
        elif field == "loc":
            await svc.update_field(tid, "location", message.text.strip())
        elif field in ("max", "dur"):
            if not message.text.isdigit() or int(message.text) < 1:
                await message.answer("Введите положительное число."); return
            f = "max_participants" if field == "max" else "duration_min"
            await svc.update_field(tid, f, int(message.text))
        training = await svc.repo.get_training(tid)
        card = await views.training_card(svc, training)
        tenant_id = data["tenant_id"]
    await state.clear()
    await message.answer("✅ Изменено:\n\n" + card,
                         reply_markup=_kb(tid, True), parse_mode="HTML")
    await _refresh_group_card(tenant_id, tid)


@router.callback_query(F.data.startswith("gu:"))
async def cb_guest_start(query: CallbackQuery, state: FSMContext) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, _ = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None:
            await query.answer("Чат не привязан к клубу.", show_alert=True); return
        svc = BookingService(session, tid)
        # записать гостя может любой записанный участник
        mine = await svc.repo.get_user_signup(train_id, PLATFORM, query.from_user.id)
        if mine is None:
            await query.answer("Сначала запишитесь сами, потом можно добавить гостя.",
                               show_alert=True); return
    await state.update_data(tenant_id=tid, train_id=train_id)
    await state.set_state(GuestSignup.name)
    await query.answer()
    await query.message.answer("Введите имя гостя, которого записываете "
                               "(он подтвердит участие у тренера):")


@router.message(GuestSignup.name)
async def guest_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    name = message.text.strip()
    if not name:
        await message.answer("Имя пустое, попробуйте ещё раз."); return
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        res = await svc.sign_up_guest(data["train_id"], name, message.from_user.id)
    await state.clear()
    if res.result == "active":
        await message.answer(f"👤 Гость «{name}» записан и занял место.\n"
                             f"⏳ Статус: требует подтверждения тренером.")
    elif res.result == "queue":
        await message.answer(f"👤 Гость «{name}» поставлен в очередь №{res.position}.\n"
                             f"⏳ Требует подтверждения тренером.")
    else:
        await message.answer("Запись закрыта или тренировка отменена.")


async def _admin_guard(message: Message) -> int | None:
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, message.chat.id, message.from_user.id)
    if tid is None or not is_admin:
        await message.answer("Команда доступна только администратору клуба."); return None
    return tid


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    tid = await _admin_guard(message)
    if tid is None: return
    await state.update_data(tenant_id=tid)
    await state.set_state(NewTraining.title)
    await message.answer("Название тренировки?")


@router.message(NewTraining.title)
async def new_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text)
    await _ask_date(message, state)


_WD_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_WD_FULL = ["понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье"]


async def _ask_date(message: Message, state: FSMContext) -> None:
    """Показывает кнопки выбора даты."""
    tz = ZoneInfo("Europe/Moscow")
    today = dt.datetime.now(tz).date()
    rows = [[
        InlineKeyboardButton(text="Сегодня", callback_data=f"nd:{today.isoformat()}"),
        InlineKeyboardButton(text="Завтра",
                             callback_data=f"nd:{(today+dt.timedelta(days=1)).isoformat()}"),
    ]]
    # ближайшие 5 дней недели с подписями
    day_row = []
    for i in range(2, 7):
        d = today + dt.timedelta(days=i)
        day_row.append(InlineKeyboardButton(
            text=f"{_WD_RU[d.weekday()]} {d.day:02d}.{d.month:02d}",
            callback_data=f"nd:{d.isoformat()}"))
        if len(day_row) == 2:
            rows.append(day_row); day_row = []
    if day_row:
        rows.append(day_row)
    rows.append([InlineKeyboardButton(text="📅 Другая дата", callback_data="nd:manual")])
    await state.set_state(NewTraining.date)
    await message.answer("📅 Выберите дату тренировки:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(NewTraining.date, F.data.startswith("nd:"))
async def new_date_cb(query: CallbackQuery, state: FSMContext) -> None:
    val = query.data.split(":", 1)[1]
    await query.answer()
    if val == "manual":
        await state.set_state(NewTraining.date_manual)
        await query.message.answer("Введите дату: ДД.ММ.ГГГГ (напр. 20.07.2026)")
        return
    await state.update_data(date=val)
    await _ask_time(query.message, state)


@router.message(NewTraining.date_manual)
async def new_date_manual(message: Message, state: FSMContext) -> None:
    txt = message.text.strip()
    try:
        d = dt.datetime.strptime(txt, "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Неверный формат. Пример: 20.07.2026"); return
    await state.update_data(date=d.isoformat())
    await _ask_time(message, state)


async def _ask_time(message: Message, state: FSMContext) -> None:
    """Кнопки выбора времени: подсказки по дню недели + частые."""
    data = await state.get_data()
    chosen = dt.date.fromisoformat(data["date"])
    weekday = chosen.weekday()

    suggested: list[str] = []
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        suggested = await svc.times_for_weekday(weekday)

    rows = []
    if suggested:
        rows.append([InlineKeyboardButton(text=f"⭐ {t}", callback_data=f"nt:{t}")
                     for t in suggested])
    # частые времена
    common = ["18:00", "19:00", "20:00", "21:00"]
    row = [InlineKeyboardButton(text=t, callback_data=f"nt:{t}") for t in common]
    rows.append(row)
    rows.append([InlineKeyboardButton(text="🕐 Другое время", callback_data="nt:manual")])

    hint = (f"🕐 Время в {_WD_FULL[weekday]}, {chosen.day:02d}.{chosen.month:02d}.\n"
            + ("⭐ — как в прошлые разы." if suggested else "Выберите время:"))
    await state.set_state(NewTraining.time)
    await message.answer(hint, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(NewTraining.time, F.data.startswith("nt:"))
async def new_time_cb(query: CallbackQuery, state: FSMContext) -> None:
    val = query.data.split(":", 1)[1] if query.data.count(":") == 1 else query.data[3:]
    await query.answer()
    if val == "manual":
        await state.set_state(NewTraining.time_manual)
        await query.message.answer("Введите время: ЧЧ:ММ (напр. 19:30)")
        return
    await _set_datetime(query.message, state, val)


@router.message(NewTraining.time_manual)
async def new_time_manual(message: Message, state: FSMContext) -> None:
    txt = message.text.strip()
    try:
        dt.datetime.strptime(txt, "%H:%M")
    except ValueError:
        await message.answer("Неверный формат. Пример: 19:30"); return
    await _set_datetime(message, state, txt)


async def _set_datetime(message: Message, state: FSMContext, hhmm: str) -> None:
    data = await state.get_data()
    tz = ZoneInfo("Europe/Moscow")
    d = dt.date.fromisoformat(data["date"])
    h, m = map(int, hhmm.split(":"))
    start = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
    await state.update_data(start_at=start.isoformat())
    await _ask_location(message, state)


async def _ask_location(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        places = await svc.recent_locations()
    rows = []
    for p in places:
        rows.append([InlineKeyboardButton(text=f"📍 {p}", callback_data=f"nl:{p[:50]}")])
    rows.append([InlineKeyboardButton(text="✏️ Другое место", callback_data="nl:manual")])
    rows.append([InlineKeyboardButton(text="➖ Без места", callback_data="nl:none")])
    await state.set_state(NewTraining.location)
    await message.answer("📍 Место тренировки:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(NewTraining.location, F.data.startswith("nl:"))
async def new_location_cb(query: CallbackQuery, state: FSMContext) -> None:
    val = query.data[3:]
    await query.answer()
    if val == "manual":
        await state.set_state(NewTraining.location_manual)
        await query.message.answer("Введите название места:")
        return
    loc = "" if val == "none" else val
    await state.update_data(location=loc)
    await _ask_duration(query.message, state)


@router.message(NewTraining.location_manual)
async def new_location_manual(message: Message, state: FSMContext) -> None:
    await state.update_data(location=message.text.strip())
    await _ask_duration(message, state)


async def _ask_duration(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        common = await svc.common_values()
    # подсказки из истории + стандартные
    durs = common["dur"] or []
    standard = [60, 90, 120, 180]
    options, seen = [], set()
    for d in durs + standard:
        if d not in seen:
            seen.add(d); options.append(d)
        if len(options) >= 4:
            break
    row = [InlineKeyboardButton(
        text=f"{'⭐ ' if d in durs else ''}{d//60} ч" if d % 60 == 0
             else f"{'⭐ ' if d in durs else ''}{d} мин",
        callback_data=f"ndur:{d}") for d in options]
    rows = [row, [InlineKeyboardButton(text="✏️ Другое", callback_data="ndur:manual")]]
    await state.set_state(NewTraining.duration)
    await message.answer("⏱ Длительность тренировки:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(NewTraining.duration, F.data.startswith("ndur:"))
async def new_duration_cb(query: CallbackQuery, state: FSMContext) -> None:
    val = query.data.split(":")[1]
    await query.answer()
    if val == "manual":
        await state.set_state(NewTraining.duration_manual)
        await query.message.answer("Введите длительность в минутах (напр. 120):")
        return
    await state.update_data(duration=int(val))
    await _ask_maxp(query.message, state)


@router.message(NewTraining.duration_manual)
async def new_duration_manual(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите число минут, напр. 120."); return
    await state.update_data(duration=int(message.text))
    await _ask_maxp(message, state)


async def _ask_maxp(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        common = await svc.common_values()
    maxes = common["max"] or []
    standard = [4, 6, 8, 12]
    options, seen = [], set()
    for m in maxes + standard:
        if m not in seen:
            seen.add(m); options.append(m)
        if len(options) >= 4:
            break
    row = [InlineKeyboardButton(
        text=f"{'⭐ ' if m in maxes else ''}{m}", callback_data=f"nmax:{m}")
        for m in options]
    rows = [row, [InlineKeyboardButton(text="✏️ Другое", callback_data="nmax:manual")]]
    await state.set_state(NewTraining.maxp)
    await message.answer("👥 Максимум участников:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(NewTraining.maxp, F.data.startswith("nmax:"))
async def new_maxp_cb(query: CallbackQuery, state: FSMContext) -> None:
    val = query.data.split(":")[1]
    await query.answer()
    if val == "manual":
        await state.set_state(NewTraining.maxp_manual)
        await query.message.answer("Введите максимум участников (число):")
        return
    await state.update_data(maxp=int(val))
    await _ask_price(query.message, state)


@router.message(NewTraining.maxp_manual)
async def new_maxp_manual(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите положительное число."); return
    await state.update_data(maxp=int(message.text))
    await _ask_price(message, state)


async def _ask_price(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        common = await svc.common_values()
    prices = [p for p in common.get("prices", []) if p > 0]
    standard = [30000, 40000, 50000]  # 300, 400, 500 руб в копейках
    options, seen = [], set()
    for p in prices + standard:
        if p not in seen:
            seen.add(p); options.append(p)
        if len(options) >= 3:
            break
    row = [InlineKeyboardButton(
        text=f"{'⭐ ' if p in prices else ''}{p // 100}₽", callback_data=f"npr:{p}")
        for p in options]
    rows = [
        [InlineKeyboardButton(text="🆓 Бесплатно", callback_data="npr:0")],
        row,
        [InlineKeyboardButton(text="✏️ Другая сумма", callback_data="npr:manual")],
    ]
    await state.set_state(NewTraining.price)
    await message.answer("💰 Стоимость участия:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(NewTraining.price, F.data.startswith("npr:"))
async def new_price_cb(query: CallbackQuery, state: FSMContext) -> None:
    val = query.data.split(":")[1]
    await query.answer()
    if val == "manual":
        await state.set_state(NewTraining.price_manual)
        await query.message.answer("Введите стоимость в рублях (напр. 350):")
        return
    await state.update_data(price_minor=int(val))
    await _ask_pubmode(query.message, state)


@router.message(NewTraining.price_manual)
async def new_price_manual(message: Message, state: FSMContext) -> None:
    txt = message.text.strip().replace("₽", "").replace("руб", "").strip()
    if not txt.isdigit():
        await message.answer("Введите число рублей, напр. 350."); return
    await state.update_data(price_minor=int(txt) * 100)
    await _ask_pubmode(message, state)


async def _ask_pubmode(message: Message, state: FSMContext) -> None:
    await state.set_state(NewTraining.pubmode)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Открыть сразу", callback_data="pm:now")],
        [InlineKeyboardButton(text="📝 Черновик", callback_data="pm:draft")],
        [InlineKeyboardButton(text="⏰ По таймеру", callback_data="pm:timer")]])
    await message.answer("Когда открыть запись?", reply_markup=kb)


async def _finalize(state: FSMContext, st: str, publish_at):
    data = await state.get_data()
    tenant_id = data["tenant_id"]
    async with SessionLocal() as session:
        svc = BookingService(session, tenant_id)
        training = await svc.create_training(
            title=data["title"], start_at=dt.datetime.fromisoformat(data["start_at"]),
            location=data["location"], max_participants=data["maxp"],
            duration_min=data["duration"], state=st, publish_at=publish_at,
            platform=PLATFORM, user_id=0)
        # цена (если указана) — отдельно, как в API
        price = data.get("price_minor", 0)
        if price:
            training.price_minor = price
            await session.commit()
        tid = training.id
        card = await views.training_card(svc, training)
    await state.clear()
    return card, tenant_id, tid


@router.callback_query(NewTraining.pubmode, F.data.startswith("pm:"))
async def new_pubmode(query: CallbackQuery, state: FSMContext) -> None:
    mode = query.data.split(":")[1]
    await query.answer()
    if mode == "now":
        card, tenant_id, tid = await _finalize(state, "published", None)
        await query.message.edit_text("Создано, запись открыта:")
        await query.message.answer(card, parse_mode="HTML")
        await _publish_to_group(tenant_id, tid)  # сразу в группу клуба
    elif mode == "draft":
        card, _, _ = await _finalize(state, "draft", None)
        await query.message.edit_text("Черновик создан (/drafts чтобы запустить):")
        await query.message.answer(card, parse_mode="HTML")
    else:
        await state.set_state(NewTraining.publish_at)
        await query.message.edit_text("Во сколько открыть запись? ДД.ММ.ГГГГ ЧЧ:ММ")


@router.message(NewTraining.publish_at)
async def new_publish_at(message: Message, state: FSMContext) -> None:
    parsed = BookingService(None, 0).parse_local(message.text)
    if not parsed:
        await message.answer("Неверный формат. Пример: 19.06.2026 09:00"); return
    card, _, _ = await _finalize(state, "draft", parsed)
    await message.answer("Запланировано, запись откроется автоматически:")
    await message.answer(card, parse_mode="HTML")


@router.message(Command("drafts"))
async def cmd_drafts(message: Message) -> None:
    tid = await _admin_guard(message)
    if tid is None: return
    async with SessionLocal() as session:
        svc = BookingService(session, tid)
        drafts = await svc.repo.list_drafts()
    if not drafts:
        await message.answer("Черновиков нет."); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"▶️ #{t.id} {t.title}", callback_data=f"pub:{t.id}")] for t in drafts])
    await message.answer("Черновики — нажмите, чтобы открыть запись:", reply_markup=kb)


@router.callback_query(F.data.startswith("pub:"))
async def cb_publish(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        await svc.publish_training(train_id)
        training = await svc.repo.get_training(train_id)
        card = await views.training_card(svc, training)
    await query.answer("Запись открыта, подписчики уведомлены.")
    await query.message.edit_text(f"Тренировка #{train_id} опубликована.")
    await query.message.answer(card, parse_mode="HTML")
    await _publish_to_group(tid, train_id)  # публикуем в группу клуба


@router.message(Command("guests"))
async def cmd_guests(message: Message) -> None:
    tid = await _admin_guard(message)
    if tid is None: return
    async with SessionLocal() as session:
        svc = BookingService(session, tid)
        trainings = await svc.repo.list_upcoming()
        # собираем тренировки, где есть неподтверждённые гости
        items = []
        for t in trainings:
            guests = await svc.list_unconfirmed_guests(t.id)
            if guests:
                items.append((t, guests))
    if not items:
        await message.answer("Неподтверждённых гостей нет."); return
    for t, guests in items:
        rows = []
        for g in guests:
            st = "осн." if g.status == "active" else "очередь"
            rows.append([
                InlineKeyboardButton(text=f"✅ {g.name} ({st})", callback_data=f"gok:{g.id}"),
                InlineKeyboardButton(text="❌ отклонить", callback_data=f"gno:{g.id}"),
            ])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.answer(f"🏸 {t.title} — гости, требующие подтверждения:",
                             reply_markup=kb)


@router.callback_query(F.data.startswith("gok:"))
async def cb_guest_confirm(query: CallbackQuery) -> None:
    sid = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        s = await svc.confirm_guest(sid)
    await query.answer(f"Гость подтверждён." if s else "Не найдено", show_alert=True)
    if s:
        await query.message.edit_text(f"✅ Гость «{s.name}» подтверждён как реально занятый.")


@router.callback_query(F.data.startswith("gno:"))
async def cb_guest_reject(query: CallbackQuery) -> None:
    sid = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        res = await svc.reject_guest(sid)
    if not res["rejected"]:
        await query.answer("Не найдено", show_alert=True); return
    await query.answer("Гость отклонён, место освобождено.", show_alert=True)
    msg = f"❌ Гость «{res['name']}» отклонён, место освобождено."
    if res.get("promoted"):
        msg += f"\n🎉 Из очереди поднят: {res['promoted'].name}."
    await query.message.edit_text(msg)


@router.message(Command("attend"))
async def cmd_attend(message: Message) -> None:
    tid = await _admin_guard(message)
    if tid is None: return
    now = dt.datetime.now(dt.timezone.utc)
    from sqlalchemy import select
    from app.models.entities import Training
    async with SessionLocal() as session:
        stmt = select(Training).where(
            Training.tenant_id == tid, Training.is_cancelled.is_(False),
            Training.start_at >= now - dt.timedelta(days=14),
            Training.start_at <= now + dt.timedelta(hours=6)).order_by(Training.start_at.desc())
        recent = list((await session.execute(stmt)).scalars())
    if not recent:
        await message.answer("Нет недавних тренировок для отметки."); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{t.id} {t.title}", callback_data=f"al:{t.id}")] for t in recent])
    await message.answer("Отметить явку/оплату — выберите тренировку:", reply_markup=kb)


async def _attend_kb(svc, train_id):
    active = await svc.repo.get_signups(train_id, "active")
    rows = []
    for s in active:
        att = "✅" if s.attended else "⬜"
        pay = "💰" if s.paid else "🚫"
        rows.append([InlineKeyboardButton(text=f"{att} {s.name}", callback_data=f"at:{s.id}"),
                     InlineKeyboardButton(text=f"{pay} оплата", callback_data=f"pa:{s.id}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"al:{train_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_attend(query, svc, train_id):
    training = await svc.repo.get_training(train_id)
    summ = await svc.training_attendance(train_id)
    kb = await _attend_kb(svc, train_id)
    text = views.attendance_summary(svc, training, summ)
    await query.message.edit_text(text + "\n\n✅ пришёл, 💰 оплатил:", reply_markup=kb)


@router.callback_query(F.data.startswith("al:"))
async def cb_attend_list(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        await query.answer()
        await _render_attend(query, svc, train_id)


@router.callback_query(F.data.startswith("at:"))
async def cb_toggle_attend(query: CallbackQuery) -> None:
    sid = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        s = await svc.toggle_attended(sid)
        await query.answer("Отмечено")
        if s:
            await _render_attend(query, svc, s.training_id)


@router.callback_query(F.data.startswith("pa:"))
async def cb_toggle_pay(query: CallbackQuery) -> None:
    sid = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        s = await svc.toggle_paid(sid)
        await query.answer("Отмечено")
        if s:
            await _render_attend(query, svc, s.training_id)


@router.message(Command("debtors"))
async def cmd_debtors(message: Message) -> None:
    if not features.statistics:
        await message.answer("💰 Учёт должников доступен в версии Pro."); return
    tid = await _admin_guard(message)
    if tid is None: return
    async with SessionLocal() as session:
        svc = BookingService(session, tid)
        debtors = await svc.list_debtors()
    text = views.debtors_text(debtors)
    if not debtors:
        await message.answer(text); return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📨 Напомнить всем", callback_data="rd")]])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "rd")
async def cb_remind_debtors(query: CallbackQuery) -> None:
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        n = await svc.remind_debtors()
    await query.answer(f"Напоминания поставлены в очередь: {n}", show_alert=True)
    await query.message.edit_text(f"📨 Напоминания отправлены {n} должникам.")


@router.message(Command("setmax"))
async def cmd_setmax(message: Message, state: FSMContext) -> None:
    tid = await _admin_guard(message)
    if tid is None: return
    async with SessionLocal() as session:
        svc = BookingService(session, tid)
        trainings = await svc.repo.list_upcoming(include_drafts=True)
    if not trainings:
        await message.answer("Нет тренировок."); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{t.id} {t.title}", callback_data=f"sm:{t.id}")] for t in trainings])
    await state.update_data(tenant_id=tid)
    await message.answer("Какой тренировке менять лимит?", reply_markup=kb)


@router.callback_query(F.data.startswith("sm:"))
async def cb_setmax_pick(query: CallbackQuery, state: FSMContext) -> None:
    train_id = int(query.data.split(":")[1])
    await state.update_data(train_id=train_id)
    await state.set_state(SetMax.value)
    await query.answer()
    await query.message.answer("Новый лимит участников?")


@router.message(SetMax.value)
async def setmax_value(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите положительное число."); return
    data = await state.get_data()
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        await svc.set_max_participants(data["train_id"], int(message.text))
        training = await svc.repo.get_training(data["train_id"])
        card = await views.training_card(svc, training)
    await state.clear()
    await message.answer(f"Лимит обновлён: {message.text}.")
    await message.answer(card)


@router.message(Command("cancel"))
async def cmd_cancel_training(message: Message) -> None:
    tid = await _admin_guard(message)
    if tid is None: return
    async with SessionLocal() as session:
        svc = BookingService(session, tid)
        trainings = await svc.repo.list_upcoming(include_drafts=True)
    if not trainings:
        await message.answer("Нет тренировок."); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{t.id} {t.title}", callback_data=f"ct:{t.id}")] for t in trainings])
    await message.answer("Какую тренировку отменить?", reply_markup=kb)


@router.callback_query(F.data.startswith("ct:"))
async def cb_cancel_training(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        await svc.cancel_training(train_id)
    await query.answer("Отменено")
    await query.message.edit_text(f"Тренировка #{train_id} отменена, участники уведомлены.")


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not features.exports:
        await message.answer("📄 Экспорт списков доступен в версии Pro."); return
    tid = await _admin_guard(message)
    if tid is None: return
    async with SessionLocal() as session:
        svc = BookingService(session, tid)
        trainings = await svc.repo.list_upcoming(include_drafts=True)
    if not trainings:
        await message.answer("Нет тренировок."); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{t.id} {t.title}", callback_data=f"ex:{t.id}")] for t in trainings])
    await message.answer("Какую тренировку выгрузить?", reply_markup=kb)


@router.callback_query(F.data.startswith("ex:"))
async def cb_export(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None or not is_admin:
            await query.answer("Только для администратора.", show_alert=True); return
        svc = BookingService(session, tid)
        csv_text = await svc.export_training_csv(train_id)
    if not csv_text:
        await query.answer("Не найдено", show_alert=True); return
    await query.answer()
    await query.message.answer_document(
        BufferedInputFile(csv_text.encode("utf-8-sig"), f"training_{train_id}.csv"),
        caption="Список участников (CSV).")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    tid = await _admin_guard(message)
    if tid is None: return
    await state.update_data(tenant_id=tid)
    await state.set_state(Broadcast.text)
    await message.answer("Текст рассылки (уйдёт всем подписчикам клуба):")


@router.message(Broadcast.text)
async def broadcast_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        counts = await svc.broadcast(message.text)
    await state.clear()
    await message.answer(f"Рассылка в очереди. Telegram: {counts['tg']}, ВКонтакте: {counts['vk']}.")


async def _publish_to_group(tenant_id: int, training_id: int) -> None:
    """
    Публикует карточку тренировки с кнопками записи в группу клуба и
    запоминает id сообщения, чтобы потом обновлять его при изменениях.
    Молча пропускает, если группа не привязана или отправка не удалась.
    """
    if not _bot:
        return
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        tenant = await g.get_tenant(tenant_id)
        if not tenant or not tenant.tg_chat_id or tenant.tg_chat_id == -100:
            return  # группа не привязана
        svc = BookingService(session, tenant_id)
        training = await svc.repo.get_training(training_id)
        if not training:
            return
        card = await views.training_card(svc, training)
        chat_id = tenant.tg_chat_id
    try:
        msg = await _bot.send_message(
            chat_id,
            "📣 <b>Новая тренировка — открыта запись!</b>\n\n" + card,
            reply_markup=_kb(training_id, is_admin=False),
            parse_mode="HTML")
        # запоминаем id сообщения для будущих обновлений
        async with SessionLocal() as session:
            svc = BookingService(session, tenant_id)
            tr = await svc.repo.get_training(training_id)
            if tr:
                tr.group_message_id = msg.message_id
                await session.commit()
    except Exception as e:
        logger.warning("Не удалось опубликовать тренировку в группу %s: %s",
                       chat_id, e)


async def _refresh_group_card(tenant_id: int, training_id: int) -> None:
    """
    Обновляет ранее опубликованную в группе карточку тренировки
    (счётчик мест, очередь, изменённые время/место). Тихо пропускает,
    если карточки в группе нет или сообщение недоступно.
    """
    if not _bot:
        return
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        tenant = await g.get_tenant(tenant_id)
        if not tenant or not tenant.tg_chat_id or tenant.tg_chat_id == -100:
            return
        svc = BookingService(session, tenant_id)
        training = await svc.repo.get_training(training_id)
        if not training or not training.group_message_id:
            return
        card = await views.training_card(svc, training)
        chat_id = tenant.tg_chat_id
        msg_id = training.group_message_id
    try:
        await _bot.edit_message_text(
            "📣 <b>Тренировка — запись открыта!</b>\n\n" + card,
            chat_id=chat_id, message_id=msg_id,
            reply_markup=_kb(training_id, is_admin=False),
            parse_mode="HTML")
    except Exception:
        pass  # не изменилось / удалено — не критично


async def _send(user_id: int, text: str) -> None:
    if _bot:
        await _bot.send_message(user_id, text)


async def setup() -> None:
    global _bot, _dp
    if not settings.tg_token:
        logger.warning("TG_TOKEN не задан — Telegram отключён."); return
    if _dp is not None:
        # уже настроен (повторный вызов, напр. в тестах) — не подключаем router снова
        return
    _bot = Bot(token=settings.tg_token)
    _dp = Dispatcher()
    _dp.include_router(router)
    tasks.register_sender(PLATFORM, _send)
    logger.info("Telegram готов (режим: %s)", settings.tg_mode)


async def run_polling() -> None:
    if _bot and _dp:
        await _dp.start_polling(_bot)


async def feed_webhook_update(update: dict) -> None:
    if _bot and _dp:
        await _dp.feed_update(_bot, Update.model_validate(update))
