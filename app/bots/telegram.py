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
from app.bots.user_info import fetch_tg_photo_ref
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


async def _tenant_suspended_msg(session, tid: int) -> str | None:
    """SaaS: если клуб приостановлен — текст для пользователя, иначе None."""
    from app.core.config import tenant_suspended
    t = await GlobalRepository(session).get_tenant(tid)
    if t is not None and tenant_suspended(t):
        return ("⏸ Работа клуба временно приостановлена. "
                "Обратитесь к тренеру.")
    return None


async def _is_admin_for(session, tenant, user_id: int) -> bool:
    if tenant.admin_tg_id == user_id:
        return True
    if tenant.is_demo:
        # демо-клуб: админом также считается любой, кто выбрал «Я тренер»
        # (Membership с role=coach/owner) — см. cmd_start и cb_demo_role.
        from app.repositories.repo import TenantRepository
        m = await TenantRepository(session, tenant.id).get_membership(user_id)
        return bool(m and m.role in ("owner", "coach"))
    return False


async def _resolve_tenant(session, chat_id: int, user_id: int):
    g = GlobalRepository(session)
    # мультиклиент: событие пришло клиентскому боту -> его клуб, без поиска
    ctx_tid = _ctx_tenant.get()
    if ctx_tid is not None:
        t = await g.get_tenant(ctx_tid)
        if t is not None:
            return t.id, await _is_admin_for(session, t, user_id)
    tenant = await g.get_tenant_by_tg_chat(chat_id)
    if tenant is None:
        for t in await g.list_tenants():
            if t.admin_tg_id == user_id:
                tenant = t
                break
    if tenant is None:
        return None, False
    return tenant.id, await _is_admin_for(session, tenant, user_id)


def _name(x) -> str:
    u = x.from_user
    return u.full_name or (u.username or f"id{u.id}")


def _username(x) -> str | None:
    return x.from_user.username  # без @ (может быть None)


# Тексты кнопок постоянного меню (внизу экрана)
BTN_LIST = "🏸 Тренировки"
BTN_MY = "📅 Мои записи"
BTN_PROFILE = "👤 Профиль"
BTN_STATS = "📊 Статистика"
BTN_RATING = "🏆 Рейтинг"
BTN_MORE = "⋯ Ещё"
BTN_BACK = "⬅️ Назад"
BTN_NEW = "➕ Создать тренировку"
BTN_ATTEND = "✅ Явки"
BTN_GUESTS = "👤 Записать гостя"
BTN_DRAFTS = "📋 Черновики"
BTN_BROADCAST = "📢 Рассылка"
BTN_NAMES = "✏️ Имена"
BTN_SCHED = "📆 Расписание"
BTN_REMIND = "⏰ Напоминание"


def _menu(is_admin: bool, more: bool = False) -> ReplyKeyboardMarkup:
    """Меню внизу экрана. У админа два экрана: основной и «⋯ Ещё»."""
    B = KeyboardButton
    if not is_admin:
        rows = [[B(text=BTN_LIST), B(text=BTN_MY)],
                [B(text=BTN_RATING), B(text=BTN_PROFILE)]]
    elif not more:
        rows = [[B(text=BTN_NEW)],                       # широкая
                [B(text=BTN_LIST), B(text=BTN_SCHED)],
                [B(text=BTN_BROADCAST), B(text=BTN_GUESTS)],
                [B(text=BTN_ATTEND), B(text=BTN_MORE)]]
    else:
        rows = [[B(text=BTN_MY), B(text=BTN_RATING)],
                [B(text=BTN_PROFILE), B(text=BTN_STATS)],
                [B(text=BTN_NAMES), B(text=BTN_DRAFTS)],
                [B(text=BTN_REMIND)],
                [B(text=BTN_BACK)]]
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
                photo = await fetch_tg_photo_ref(_bot, uid)
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


def _kb(tid: int, is_admin: bool = False,
        is_full: bool = False) -> InlineKeyboardMarkup:
    # когда мест нет — кнопка честно предлагает встать в очередь
    signup_text = "⏳ Встать в очередь" if is_full else "✅ Записаться"
    # кнопка обновления — самой первой, наверху; доступна всем участникам
    # (не только админу), чтобы видеть актуальный список записавшихся
    rows = [[
        InlineKeyboardButton(text="🔄 Обновить список", callback_data=f"ref:{tid}"),
    ]]
    rows.append([
        InlineKeyboardButton(text=signup_text, callback_data=f"su:{tid}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"cx:{tid}"),
    ])
    rows.append([
        InlineKeyboardButton(text="👤 Записать гостя", callback_data=f"gu:{tid}"),
    ])
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


async def _is_full(svc, training) -> bool:
    """True, если активных записей не меньше лимита мест."""
    active = await svc.repo.get_signups(training.id, "active")
    return len(active) >= training.max_participants


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


def _demo_role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎓 Я тренер", callback_data="demo:coach"),
        InlineKeyboardButton(text="🙋 Я участник", callback_data="demo:participant"),
    ]])


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    needs_role_pick = False
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Этот чат не привязан к клубу. Обратитесь к администратору платформы.")
            return
        _sus = await _tenant_suspended_msg(session, tid)
        if _sus:
            await message.answer(_sus); return
        # обновление профиля не должно мешать приветствию
        try:
            svc = BookingService(session, tid)
            await _upsert_user(svc, message.from_user)
            await svc.repo.set_subscription(PLATFORM, message.from_user.id, True)
            tenant = await GlobalRepository(session).get_tenant(tid)
            if tenant and tenant.is_demo and not is_admin:
                existing = await svc.repo.get_membership(message.from_user.id)
                needs_role_pick = existing is None
            await session.commit()
        except Exception as e:
            logger.warning("Не удалось обновить профиль при /start: %s", e)

    if needs_role_pick:
        await message.answer(
            "🧪 <b>Демо-версия бота</b>\n\n"
            "Здесь можно попробовать бота и как тренер, и как участник — "
            "выберите роль. Демо-клуб каждую ночь обновляется заново, "
            "так что можно нажимать что угодно.",
            reply_markup=_demo_role_kb(), parse_mode="HTML")
        return
    custom = None
    try:
        async with SessionLocal() as _s:
            from app.repositories.repo import GlobalRepository as _G
            _t = await _G(_s).get_tenant(tid)
            custom = (_t.welcome_text or "").strip() if _t else None
    except Exception:
        custom = None
    text = custom or (
        "🏸 <b>Добро пожаловать!</b>\n\n"
        "Это бот для записи на тренировки. Через меню внизу можно "
        "посмотреть тренировки, записаться и увидеть свою статистику.\n\n"
        "👇 Используйте кнопки меню под полем ввода."
    )
    if is_admin:
        try:
            async with SessionLocal() as _s2:
                _ob = await views.onboarding_text(BookingService(_s2, tid))
            if _ob:
                text += "\n\n" + _ob
        except Exception:
            pass
        text += ("\n\n🛠 <b>Вы администратор клуба.</b>\n"
                 "Вам доступны кнопки создания тренировок, отметки явки, "
                 "подтверждения гостей, черновиков и рассылки.")
    await message.answer(text, reply_markup=_menu(is_admin), parse_mode="HTML")


@router.callback_query(F.data.in_(("demo:coach", "demo:participant")))
async def cb_demo_role(query: CallbackQuery) -> None:
    """Выбор роли в демо-клубе (см. cmd_start): «тренер» получает Membership
    role=coach (см. _is_admin_for), «участник» — обычный поток без изменений."""
    as_coach = query.data == "demo:coach"
    async with SessionLocal() as session:
        tid, _ = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None:
            await query.answer("Чат не привязан к клубу.", show_alert=True); return
        tenant = await GlobalRepository(session).get_tenant(tid)
        if not (tenant and tenant.is_demo):
            await query.answer(); return
        if as_coach:
            svc = BookingService(session, tid)
            await svc.repo.upsert_membership(query.from_user.id, "coach", _name(query))
            await session.commit()
    if as_coach:
        await query.answer("Вы — тренер демо-клуба ✅")
        await query.message.edit_text(
            "🎓 <b>Вы тренер демо-клуба.</b>\nСоздавайте тренировки, "
            "отмечайте явку и оплату — всё как у настоящего клуба.",
            parse_mode="HTML")
        await query.message.answer("👇 Меню тренера:", reply_markup=_menu(True))
    else:
        await query.answer("Вы участник демо-клуба ✅")
        await query.message.edit_text(
            "🙋 <b>Вы участник демо-клуба.</b>\nЗаписывайтесь на тренировки и "
            "смотрите статистику — как обычный клиент клуба.", parse_mode="HTML")
        await query.message.answer("👇 Меню участника:", reply_markup=_menu(False))


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
        rows = await svc.my_trainings(PLATFORM, message.from_user.id)
        if not rows:
            await message.answer("📭 Вы не записаны ни на одну предстоящую тренировку.\n"
                                 "Нажмите «🏸 Тренировки», чтобы записаться."); return
        as_admin = is_admin and message.chat.type == "private"
        cards = []
        for training, status, position in rows:
            card = await views.training_card(svc, training, for_admin=as_admin)
            mark = ("✅ Вы записаны" if status == "active"
                    else f"⏳ Вы в очереди (№{position})")
            full = await _is_full(svc, training)
            cards.append((training.id, f"{mark}\n\n{card}", full))
    await message.answer("📅 <b>Ваши записи:</b>", parse_mode="HTML")
    for tid_, text, full in cards:
        await message.answer(text, reply_markup=_kb(tid_, is_admin, full),
                             parse_mode="HTML")
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
        _sus = await _tenant_suspended_msg(session, tid)
        if _sus:
            await message.answer(_sus); return
        svc = BookingService(session, tid)
        trainings = await svc.repo.list_upcoming()
        if not trainings:
            await message.answer("Ближайших тренировок нет."); return
        for t in trainings:
            full = await _is_full(svc, t)
            as_admin = is_admin and message.chat.type == "private"
            await message.answer(
                await views.training_card(svc, t, for_admin=as_admin),
                reply_markup=_kb(t.id, is_admin, full),
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
        summary = await svc.monthly_summary()
        past = await svc.repo.list_past(limit=8)
        debtors = await svc.list_debtors()
    if not rows and not past:
        await message.answer("Пока нет данных о посещениях."); return
    lines = [views.ranking_text(rows) if rows else "Пока нет данных о посещениях."]
    if debtors:
        total = sum(d["debts"] for d in debtors)
        lines.append(f"\n💰 Должников: {len(debtors)} (долгов: {total})")
    if summary:
        lines.append("\n📅 По месяцам:")
        for r in summary:
            y, m = r["month"].split("-")
            lines.append(f"  {m}.{y}: тренировок {r['trainings']}, "
                         f"посещений {r['attended']}")
    if past:
        lines.append("\n📜 Прошедшие тренировки:")
        for t in past:
            lines.append(f"  • {t.title} — {svc.format_local(t.start_at)}")
    await message.answer("\n".join(lines))
    if rows:
        png = charts.attendance_chart_png(rows)
        if png:
            await message.answer_photo(
                BufferedInputFile(png, "attendance.png"), caption="Посещаемость")


@router.message(F.text == BTN_RATING)
async def btn_rating(message: Message) -> None:
    """Топ посещаемости — доступен всем участникам."""
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(
            session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Чат не привязан к клубу."); return
        svc = BookingService(session, tid)
        rows = await svc.attendance_ranking()
        me = await svc.user_stats(PLATFORM, message.from_user.id)
    if not rows:
        await message.answer("Пока нет данных о посещениях."); return
    text = views.ranking_text(rows)
    if me and me.get("attended"):
        text += f"\n\n👤 Вы: посещений {me.get('attended', 0)}"
    await message.answer(text)


@router.callback_query(F.data.startswith("ref:"))
async def cb_refresh(query: CallbackQuery) -> None:
    """Кнопка «Обновить» — перечитывает карточку с актуальным списком."""
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None:
            await query.answer("Чат не привязан к клубу.", show_alert=True); return
        svc = BookingService(session, tid)
        training = await svc.repo.get_training(train_id)
        if not training:
            await query.answer("Тренировка не найдена.", show_alert=True); return
        as_admin = is_admin and query.message.chat.type == "private"
        card = await views.training_card(svc, training, for_admin=as_admin)
        full = await _is_full(svc, training)
    await query.answer("Обновлено")
    try:
        await query.message.edit_text(
            card, reply_markup=_kb(train_id, is_admin, full), parse_mode="HTML")
    except Exception:
        pass  # данные не изменились — Telegram вернёт ошибку, это ок


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
        as_admin = is_admin and query.message.chat.type == "private"
        new_card = await views.training_card(svc, training, for_admin=as_admin) if training else None
        full = await _is_full(svc, training) if training else False
    await query.answer(views.signup_result(res, training.title if training else ""), show_alert=True)
    await _refresh_card(query, train_id, new_card, is_admin, is_full=full)
    await _refresh_group_card(tid, train_id)


async def _refresh_card(query, train_id: int, card: str | None,
                        is_admin: bool = False, prefix: str = "",
                        is_full: bool = False) -> None:
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
            reply_markup=_kb(train_id, is_admin, is_full), parse_mode="HTML")
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
        as_admin = is_admin and query.message.chat.type == "private"
        new_card = await views.training_card(svc, training, for_admin=as_admin) if training else None
        full = await _is_full(svc, training) if training else False
    if res.get("locked"):
        await query.answer(
            f"Отмена закрыта: до тренировки меньше {res['lock_minutes']} мин. "
            f"Свяжитесь с тренером.", show_alert=True)
        return
    await query.answer("Запись отменена." if res["cancelled"] else "Вы не были записаны.", show_alert=True)
    if res["cancelled"]:
        await _refresh_card(query, train_id, new_card, is_admin, is_full=full)
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


async def notify_group_cancelled(tenant_id: int, title: str, when: str) -> None:
    """Публикует в Telegram-группу клуба сообщение об отмене тренировки.
    Вызывается и из Telegram, и из VK. Тихо пропускает, если группы нет."""
    if not _bot:
        return
    async with SessionLocal() as session:
        g = GlobalRepository(session)
        tenant = await g.get_tenant(tenant_id)
        group_chat = tenant.tg_chat_id if tenant else None
    if group_chat and group_chat != -100:
        try:
            await _bot_for(tenant_id).send_message(
                group_chat,
                f"🚫 <b>Тренировка отменена</b>\n{title} — {when}",
                parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось уведомить группу об отмене: %s", e)


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
    if _bot_for(tid) and group_chat and group_chat != -100:
        try:
            await _bot_for(tid).send_message(
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
        # уведомляем записанных об изменении
        readable = {"time": "🕐 Новые дата/время", "loc": "📍 Новое место",
                    "max": "👥 Новый лимит", "dur": "⏱ Новая длительность"}.get(
                        field, "Изменение")
        try:
            await svc.notify_changed(tid, readable)
        except Exception as e:
            logger.warning("Не удалось уведомить об изменении: %s", e)
        training = await svc.repo.get_training(tid)
        card = await views.training_card(svc, training, for_admin=True)
        tenant_id = data["tenant_id"]
        full = await _is_full(svc, training) if training else False
    await state.clear()
    await message.answer("✅ Изменено:\n\n" + card,
                         reply_markup=_kb(tid, True, full), parse_mode="HTML")
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
        return
    await _refresh_group_card(data["tenant_id"], data["train_id"])


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
        await query.message.edit_text("✅ Тренировка создана, запись открыта:")
        await query.message.answer(card, parse_mode="HTML",
                                   reply_markup=_kb(tid, is_admin=True))
        await _publish_to_group(tenant_id, tid)  # сразу в группу клуба
        await _publish_to_vk(tenant_id, tid)     # анонс на стене ВК
        await _notify_subscribers_new_training(tenant_id, tid)  # личка подписчикам
    elif mode == "draft":
        card, _, tid = await _finalize(state, "draft", None)
        await query.message.edit_text("📝 Черновик создан (/drafts чтобы запустить):")
        await query.message.answer(card, parse_mode="HTML",
                                   reply_markup=_kb(tid, is_admin=True))
    else:
        await state.set_state(NewTraining.publish_at)
        await query.message.edit_text("Во сколько открыть запись? ДД.ММ.ГГГГ ЧЧ:ММ")


@router.message(NewTraining.publish_at)
async def new_publish_at(message: Message, state: FSMContext) -> None:
    parsed = BookingService(None, 0).parse_local(message.text)
    if not parsed:
        await message.answer("Неверный формат. Пример: 19.06.2026 09:00"); return
    card, _, tid = await _finalize(state, "draft", parsed)
    await message.answer("⏰ Запланировано, запись откроется автоматически:")
    await message.answer(card, parse_mode="HTML",
                         reply_markup=_kb(tid, is_admin=True))


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
    await _publish_to_vk(tid, train_id)     # анонс на стене ВК


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
    await query.answer("Гость подтверждён." if s else "Не найдено", show_alert=True)
    if s:
        await query.message.edit_text(f"✅ Гость «{s.name}» подтверждён как реально занятый.")
        await _refresh_group_card(tid, s.training_id)


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
    await _refresh_group_card(tid, res["training_id"])


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
    aliases = await svc.repo.aliases_map("tg")
    rows = []
    for s in active:
        att = "✅" if s.attended else "⬜"
        pay = "💰" if s.paid else "🚫"
        shown = aliases.get(getattr(s, "user_id", None)) or s.name
        rows.append([InlineKeyboardButton(text=f"{att} {shown}", callback_data=f"at:{s.id}"),
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
        card = await views.training_card(svc, training, for_admin=True)
    await state.clear()
    await message.answer(f"Лимит обновлён: {message.text}.")
    await message.answer(card, parse_mode="HTML")


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
async def cb_cancel_training_direct(query: CallbackQuery) -> None:
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


async def _publish_to_vk(tenant_id: int, training_id: int) -> None:
    """Публикует анонс тренировки на стене ВК-сообщества (если ВК настроен)."""
    try:
        from app.bots import vk
        await vk.publish_to_wall(tenant_id, training_id)
    except Exception as e:
        logger.warning("Не удалось опубликовать анонс в ВК: %s", e)


async def _notify_subscribers_new_training(tenant_id: int, training_id: int) -> None:
    """
    Личное уведомление в личку каждому подписчику клуба (TG и VK) о новой
    открытой тренировке. Публикация карточки в группу/на стену (см. выше)
    видят только зашедшие туда — подписчик, который просто писал боту в
    личку, узнавал о новой тренировке только сам открыв «🏸 Тренировки».
    """
    try:
        async with SessionLocal() as session:
            svc = BookingService(session, tenant_id)
            training = await svc.repo.get_training(training_id)
            if training:
                await svc.notify_new_training(training)
            await session.commit()
    except Exception as e:
        logger.warning("Не удалось разослать уведомление подписчикам: %s", e)


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
        full = await _is_full(svc, training)
    try:
        msg = await _bot_for(tenant_id).send_message(
            chat_id,
            "📣 <b>Новая тренировка — открыта запись!</b>\n\n" + card,
            reply_markup=_kb(training_id, is_admin=False, is_full=full),
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
        full = await _is_full(svc, training)
    try:
        await _bot.edit_message_text(
            "📣 <b>Тренировка — запись открыта!</b>\n\n" + card,
            chat_id=chat_id, message_id=msg_id,
            reply_markup=_kb(training_id, is_admin=False, is_full=full),
            parse_mode="HTML")
    except Exception:
        pass  # не изменилось / удалено — не критично


# ─── мультиклиент: боты клубов с собственными токенами ───
_tenant_bots: dict[int, "Bot"] = {}     # tenant_id -> Bot (из tenants.tg_token)
_token_tenants: dict[str, int] = {}     # token -> tenant_id (клиентские боты)

from contextvars import ContextVar
_ctx_tenant: ContextVar[int | None] = ContextVar("tg_tenant", default=None)

from aiogram import BaseMiddleware


class _TenantMiddleware(BaseMiddleware):
    """Определяет клуб по токену бота, принявшего событие (мультиклиент)."""
    async def __call__(self, handler, event, data):
        b = data.get("bot")
        tid = _token_tenants.get(getattr(b, "token", "") or "")
        tok = _ctx_tenant.set(tid)
        try:
            return await handler(event, data)
        finally:
            _ctx_tenant.reset(tok)



def _bot_for(tenant_id: int | None):
    """Бот конкретного клуба, либо бот по умолчанию (из env)."""
    if tenant_id is not None and tenant_id in _tenant_bots:
        return _tenant_bots[tenant_id]
    return _bot


async def _send(user_id: int, text: str, tenant_id: int | None = None) -> None:
    b = _bot_for(tenant_id)
    if b:
        await b.send_message(user_id, text)


async def send_document_to_owner(user_id: int, filename: str, data: bytes,
                                 caption: str = "") -> bool:
    """Отправляет файл владельцу площадки через ДЕФОЛТНОГО (платформенного)
    бота — используется для внешних бэкапов базы, которые не привязаны к
    конкретному клубу. Возвращает True при успехе."""
    if not _bot:
        return False
    try:
        await _bot.send_document(
            user_id, BufferedInputFile(data, filename), caption=caption)
        return True
    except Exception as e:
        logger.warning("Не удалось отправить документ владельцу площадки: %s", e)
        return False


async def send_text_to_owner(user_id: int, text: str) -> bool:
    """Текстовое уведомление владельцу площадки через ДЕФОЛТНОГО бота (не
    привязано к конкретному клубу) — например, о новом отзыве на модерации."""
    if not _bot:
        return False
    try:
        await _bot.send_message(user_id, text)
        return True
    except Exception as e:
        logger.warning("Не удалось отправить сообщение владельцу площадки: %s", e)
        return False


async def setup() -> None:
    global _bot, _dp
    if not settings.tg_token:
        logger.warning("TG_TOKEN не задан — Telegram отключён."); return
    if _dp is not None:
        # уже настроен (повторный вызов, напр. в тестах) — не подключаем router снова
        return
    _bot = Bot(token=settings.tg_token)
    _dp = Dispatcher()
    _dp.update.outer_middleware(_TenantMiddleware())
    _dp.include_router(router)
    # мультиклиент: поднимаем ботов клубов с собственными токенами
    try:
        await _load_client_bots()
        if _tenant_bots:
            logger.info("Telegram: клиентских ботов из базы: %d",
                        len(_tenant_bots))
    except Exception as e:
        logger.warning("Telegram: не удалось поднять клиентских ботов: %s", e)
    tasks.register_sender(PLATFORM, _send)
    logger.info("Telegram готов (режим: %s)", settings.tg_mode)


_reload_evt = None
_polling_active = False


async def _load_client_bots() -> None:
    """(Пере)читывает клиентских ботов из базы в реестры."""
    from sqlalchemy import select
    from app.models.entities import Tenant
    async with SessionLocal() as _s:
        tenants = list((await _s.execute(
            select(Tenant).where(Tenant.tg_token.is_not(None)))).scalars())
    fresh: dict[int, str] = {}
    for t in tenants:
        tok = (t.tg_token or "").strip()
        if tok and tok != settings.tg_token:
            fresh[t.id] = tok
    # закрываем убранных/сменивших токен
    for tid, b in list(_tenant_bots.items()):
        if fresh.get(tid) != b.token:
            try:
                await b.session.close()
            except Exception:
                pass
            _token_tenants.pop(b.token, None)
            _tenant_bots.pop(tid, None)
    # поднимаем новых
    for tid, tok in fresh.items():
        if tid not in _tenant_bots:
            try:
                _tenant_bots[tid] = Bot(token=tok)
                _token_tenants[tok] = tid
            except Exception as e:
                logger.warning("Telegram: токен клуба id=%s отклонён: %s",
                               tid, e)


async def reload_client_bots() -> None:
    """Мультиклиент: применяет токены из базы без рестарта сервиса."""
    if _dp is None:
        return
    await _load_client_bots()
    if _polling_active and _reload_evt is not None:
        _reload_evt.set()          # перезапустить поллинг с новым набором
    logger.info("Telegram: клиентские боты перечитаны (%d)", len(_tenant_bots))


async def run_polling() -> None:
    global _reload_evt, _polling_active
    if not (_bot and _dp):
        return
    import asyncio as _aio
    _reload_evt = _aio.Event()
    _polling_active = True
    try:
        while True:
            _reload_evt.clear()
            bots = [_bot, *_tenant_bots.values()]
            poll = _aio.create_task(_dp.start_polling(*bots))
            waiter = _aio.create_task(_reload_evt.wait())
            done, _ = await _aio.wait({poll, waiter},
                                      return_when=_aio.FIRST_COMPLETED)
            if waiter in done and poll not in done:
                poll.cancel()
                try:
                    await poll
                except (Exception, _aio.CancelledError):
                    pass
                logger.info("Telegram: перезапускаю поллинг с новыми ботами…")
                continue
            waiter.cancel()
            # poll завершился САМ (не из-за reload) — start_polling рассчитан
            # на бесконечную работу, значит это сбой. Пробрасываем исключение
            # наружу (await на уже завершённой задаче — не блокирует), чтобы
            # внешний супервизор (tasks.supervise) перезапустил run_polling
            # целиком, а не молча замолчал до ручного рестарта.
            return await poll
    finally:
        _polling_active = False


async def feed_webhook_update(update: dict) -> None:
    if _bot and _dp:
        await _dp.feed_update(_bot, Update.model_validate(update))


# ─────────── Регулярное расписание и напоминание (паритет с ВК) ───────────
from aiogram.types import InlineKeyboardButton as _IB

_WD_RU2 = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_WD_FULL2 = ["Понедельник", "Вторник", "Среда", "Четверг",
             "Пятница", "Суббота", "Воскресенье"]


class SchedWizard(StatesGroup):
    text = State()          # текстовые шаги: название / место / ручной ввод


async def _admin_tenant(session, user_id: int):
    from sqlalchemy import select as _sel
    from app.models.entities import Tenant as _T
    return (await session.execute(
        _sel(_T).where(_T.admin_tg_id == user_id))).scalars().first()


def _rows(btns, per_row=2):
    return InlineKeyboardMarkup(inline_keyboard=[
        btns[i:i + per_row] for i in range(0, len(btns), per_row)])


def _val_kb(field: str, options) -> InlineKeyboardMarkup:
    btns = [_IB(text=lbl, callback_data=f"scv:{field}:{v}")
            for lbl, v in options]
    btns.append(_IB(text="✏️ Ввести вручную", callback_data=f"scmn:{field}"))
    return _rows(btns)


_SC_OPTS = {
    "time": [(t, t) for t in ("18:00", "19:00", "20:00", "21:00", "10:00", "12:00")],
    "duration": [("1 ч", 60), ("1.5 ч", 90), ("2 ч", 120), ("3 ч", 180)],
    "price": [("Бесплатно", 0), ("300₽", 300), ("500₽", 500),
              ("700₽", 700), ("800₽", 800)],
    "max": [(str(n), n) for n in (2, 4, 6, 8, 10, 12)],
    "ahead": [("За 1 день", 1), ("За 2 дня", 2), ("За 3 дня", 3),
              ("За 5 дней", 5), ("За неделю", 7)],
}
_SC_ORDER = ["wd", "time", "title", "location", "duration", "price", "max", "ahead"]
_SC_PROMPTS = {
    "time": "🕐 Время занятия:", "duration": "⏱ Длительность:",
    "price": "💰 Цена:", "max": "👥 Максимум участников:",
    "ahead": "📅 За сколько дней до занятия создавать тренировку "
             "и открывать запись?",
}


async def _sc_ask(message, state: FSMContext, step: str) -> None:
    if step == "wd":
        kb = _rows([_IB(text=_WD_FULL2[i], callback_data=f"scw:{i}")
                    for i in range(7)])
        await message.answer("📆 Какой день недели?", reply_markup=kb)
    elif step in ("title", "location"):
        await state.set_state(SchedWizard.text)
        await state.update_data(field=step)
        await message.answer("📝 Название тренировки (текстом):" if step == "title"
                             else "📍 Место (текстом):")
    else:
        await message.answer(_SC_PROMPTS[step],
                             reply_markup=_val_kb(step, _SC_OPTS[step]))


async def _sc_advance(message, state: FSMContext) -> None:
    data = await state.get_data()
    cur = data.get("step")
    mode = data.get("mode")
    if mode == "edit":                      # в правке один шаг — сохраняем
        await _sc_apply_edit(message, state)
        return
    nxt = _SC_ORDER[_SC_ORDER.index(cur) + 1] if cur in _SC_ORDER[:-1] else None
    if nxt is None:
        d = data
        async with SessionLocal() as session:
            tenant = await _admin_tenant(session, d["uid"])
            svc = BookingService(session, tenant.id, tz=tenant.timezone)
            await svc.repo.add_schedule(
                weekday=d["wd"], time_str=d["time"], title=d["title"],
                location=d.get("location", ""), duration_min=int(d["duration"]),
                price_minor=int(d["price"]) * 100,
                max_participants=int(d["max"]), days_ahead=int(d["ahead"]))
            await session.commit()
        await state.clear()
        await message.answer(
            f"✅ Расписание добавлено: {_WD_FULL2[d['wd']]} {d['time']} — "
            f"«{d['title']}». Создаётся за {d['ahead']} дн. до занятия.")
        return
    await state.update_data(step=nxt)
    await _sc_ask(message, state, nxt)


async def _sc_apply_edit(message, state: FSMContext) -> None:
    d = await state.get_data()
    await state.clear()
    field, sid, val = d["field"], d["sid"], d["value"]
    async with SessionLocal() as session:
        tenant = await _admin_tenant(session, d["uid"])
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        sch = await svc.repo.get_schedule(sid)
        if not sch:
            await message.answer("Шаблон не найден."); return
        if field == "wd":
            sch.weekday = int(val); sch.last_date = ""
            label = f"день → {_WD_FULL2[int(val)]}"
        elif field == "time":
            sch.time_str = val; sch.last_date = ""
            label = f"время → {val}"
        elif field == "title":
            sch.title = val[:250]; label = f"название → {sch.title}"
        elif field == "location":
            sch.location = val[:250]; label = f"место → {sch.location}"
        elif field == "duration":
            sch.duration_min = int(val); label = f"длительность → {val} мин"
        elif field == "price":
            sch.price_minor = int(val) * 100
            label = f"цена → {val}₽" if int(val) else "цена → бесплатно"
        elif field == "max":
            sch.max_participants = int(val); label = f"лимит → {val}"
        elif field == "ahead":
            sch.days_ahead = int(val); label = f"создавать за {val} дн."
        await session.commit()
    note = (" Уже созданные тренировки не меняются." if field in ("wd", "time")
            else "")
    await message.answer(f"✅ Шаблон изменён: {label}.{note}")


@router.message(F.text == BTN_SCHED)
async def btn_sched(message: Message) -> None:
    async with SessionLocal() as session:
        tenant = await _admin_tenant(session, message.from_user.id)
        if not tenant:
            return
        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        schedules = await svc.repo.list_schedules()
    btns, lines = [], ["📆 Регулярное расписание:"]
    for n, sch in enumerate(schedules[:6], 1):
        price = f", {sch.price_minor // 100}₽" if sch.price_minor else ""
        lines.append(f"{n}. {_WD_RU2[sch.weekday]} {sch.time_str} — {sch.title} "
                     f"(макс {sch.max_participants}{price}, за {sch.days_ahead} дн.)")
        btns.append(_IB(text=f"✏️ {n}", callback_data=f"sce:{sch.id}"))
        btns.append(_IB(text=f"🗑 {n}", callback_data=f"scdel:{sch.id}"))
    if not schedules:
        lines = ["📆 Регулярного расписания пока нет.\nДобавьте шаблон — "
                 "тренировки будут создаваться автоматически каждую неделю."]
    btns.append(_IB(text="➕ Добавить", callback_data="scadd:"))
    await message.answer("\n".join(lines), reply_markup=_rows(btns))


@router.callback_query(F.data.startswith("scadd:"))
async def cb_sc_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_data({"mode": "add", "step": "wd",
                          "uid": query.from_user.id})
    await query.answer()
    await _sc_ask(query.message, state, "wd")


@router.callback_query(F.data.startswith("scdel:"))
async def cb_sc_del(query: CallbackQuery) -> None:
    sid = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tenant = await _admin_tenant(session, query.from_user.id)
        if not tenant:
            await query.answer("⛔"); return
        svc = BookingService(session, tenant.id)
        ok = await svc.repo.delete_schedule(sid)
        await session.commit()
    await query.answer("🗑 Удалено" if ok else "Не найдено")
    await query.message.edit_text("🗑 Шаблон удалён.")


@router.callback_query(F.data.startswith("sce:"))
async def cb_sc_edit(query: CallbackQuery) -> None:
    sid = query.data.split(":")[1]
    fields = [("📆 День", "wd"), ("🕐 Время", "time"), ("📝 Название", "title"),
              ("📍 Место", "location"), ("⏱ Длит.", "duration"),
              ("💰 Цена", "price"), ("👥 Лимит", "max"),
              ("📅 За сколько дней", "ahead")]
    btns = [_IB(text=t, callback_data=f"scf:{sid}:{f}") for t, f in fields]
    await query.answer()
    await query.message.answer("✏️ Что изменить в шаблоне?",
                               reply_markup=_rows(btns))


@router.callback_query(F.data.startswith("scf:"))
async def cb_sc_field(query: CallbackQuery, state: FSMContext) -> None:
    _, sid, field = query.data.split(":")
    await state.set_data({"mode": "edit", "sid": int(sid), "field": field,
                          "step": field, "uid": query.from_user.id})
    await query.answer()
    if field == "wd":
        await _sc_ask(query.message, state, "wd")
    elif field in ("title", "location"):
        await state.set_state(SchedWizard.text)
        await query.message.answer("Введите новое значение (текстом):")
    else:
        await query.message.answer(_SC_PROMPTS[field],
                                   reply_markup=_val_kb(field, _SC_OPTS[field]))


@router.callback_query(F.data.startswith("scw:"))
async def cb_sc_wd(query: CallbackQuery, state: FSMContext) -> None:
    wd = int(query.data.split(":")[1])
    d = await state.get_data()
    await query.answer(_WD_FULL2[wd])
    if d.get("mode") == "edit":
        await state.update_data(value=wd)
        await _sc_apply_edit(query.message, state)
    else:
        await state.update_data(wd=wd, step="wd")
        await _sc_advance(query.message, state)


@router.callback_query(F.data.startswith("scv:"))
async def cb_sc_val(query: CallbackQuery, state: FSMContext) -> None:
    _, field, val = query.data.split(":", 2)
    d = await state.get_data()
    await query.answer("✅")
    if d.get("mode") == "edit":
        await state.update_data(value=val)
        await _sc_apply_edit(query.message, state)
    else:
        await state.update_data(**{field: val}, step=field)
        await _sc_advance(query.message, state)


@router.callback_query(F.data.startswith("scmn:"))
async def cb_sc_manual(query: CallbackQuery, state: FSMContext) -> None:
    field = query.data.split(":")[1]
    await state.update_data(field=field)
    await state.set_state(SchedWizard.text)
    hints = {"time": "ЧЧ:ММ (напр. 19:30)", "duration": "минуты (напр. 90)",
             "price": "рубли (напр. 500 или 0)", "max": "число участников",
             "ahead": "число дней"}
    await query.answer()
    await query.message.answer(f"Введите значение: {hints.get(field, '')}")


@router.message(SchedWizard.text, F.text)
async def sc_text(message: Message, state: FSMContext) -> None:
    d = await state.get_data()
    field = d.get("field")
    text = (message.text or "").strip()
    if field == "time":
        import datetime as _d2
        try:
            _d2.datetime.strptime(text, "%H:%M")
        except ValueError:
            await message.answer("Формат: ЧЧ:ММ"); return
    elif field in ("duration", "price", "max", "ahead") and not text.isdigit():
        await message.answer("Введите число."); return
    elif field in ("title", "location"):
        text = text[:250]
    await state.set_state(None)
    if d.get("mode") == "edit":
        await state.update_data(value=text)
        await _sc_apply_edit(message, state)
    else:
        await state.update_data(**{field: text}, step=field)
        await _sc_advance(message, state)


@router.message(F.text == BTN_REMIND)
async def btn_remind(message: Message) -> None:
    async with SessionLocal() as session:
        tenant = await _admin_tenant(session, message.from_user.id)
        if not tenant:
            return
        m = tenant.reminder_minutes
        cur = ("выключено" if not tenant.reminder_enabled else
               f"за {m // 60} ч" if m % 60 == 0 else f"за {m} мин")
    opts = [("Выключить", 0), ("За 30 мин", 30), ("За 1 час", 60),
            ("За 2 часа", 120), ("За 3 часа", 180), ("За сутки", 1440)]
    kb = _rows([_IB(text=t, callback_data=f"rmset:{v}") for t, v in opts])
    await message.answer(f"⏰ Напоминание участникам: {cur}.\n"
                         "Когда напоминать записанным?", reply_markup=kb)


@router.callback_query(F.data.startswith("rmset:"))
async def cb_rmset(query: CallbackQuery) -> None:
    minutes = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tenant = await _admin_tenant(session, query.from_user.id)
        if not tenant:
            await query.answer("⛔ Только тренер"); return
        if minutes <= 0:
            tenant.reminder_enabled = False
            res = "✅ Напоминания выключены."
        else:
            tenant.reminder_enabled = True
            tenant.reminder_minutes = minutes
            human = f"{minutes // 60} ч" if minutes % 60 == 0 else f"{minutes} мин"
            res = f"✅ Напоминание: за {human} до начала."
        await session.commit()
    await query.answer("Сохранено")
    await query.message.edit_text(res)


@router.message(F.text.func(lambda t: (t or "").strip().lower() == "демо"))
async def cmd_demo(message: Message) -> None:
    """Наполняет пустой клуб примером (только админ)."""
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(
            session, message.chat.id, message.from_user.id)
        if tid is None or not is_admin:
            return
        svc = BookingService(session, tid)
        ok = await svc.seed_demo()
    if ok:
        await message.answer("✅ Демо-данные добавлены: тренировки, записи, "
                             "явка, расписание. Нажмите «🏸 Тренировки».")
    else:
        await message.answer("В клубе уже есть тренировки — демо добавляется "
                             "только в пустой клуб.")


@router.message(F.text == BTN_MORE)
async def btn_more(message: Message) -> None:
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(
            session, message.chat.id, message.from_user.id)
    if tid is None or not is_admin:
        return
    await message.answer("Дополнительно:", reply_markup=_menu(True, more=True))


@router.message(F.text == BTN_BACK)
async def btn_back(message: Message) -> None:
    async with SessionLocal() as session:
        tid, is_admin = await _resolve_tenant(
            session, message.chat.id, message.from_user.id)
    if tid is None:
        return
    await message.answer("Главное меню:", reply_markup=_menu(is_admin))
