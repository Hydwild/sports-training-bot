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

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, Update,
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


async def _upsert_user(svc: BookingService, user) -> None:
    """
    Сохраняет имя и username участника. Аватар подтягивается фоново —
    не блокирует запись, но к следующему открытию списка уже будет.
    """
    uid = user.id
    name = user.full_name or (user.username or f"id{uid}")
    uname = user.username

    # сохраняем сразу с тем, что есть
    await svc.repo.upsert_subscriber(PLATFORM, uid, name, username=uname)
    await svc.session.commit()

    # аватар запрашиваем фоново — не задерживаем ответ пользователю
    if _bot:
        async def _bg():
            photo = await fetch_tg_photo_url(_bot, uid)
            if photo:
                async with SessionLocal() as s2:
                    tid = svc.tenant_id
                    svc2 = BookingService(s2, tid)
                    await svc2.repo.upsert_subscriber(
                        PLATFORM, uid, name, username=uname, photo_url=photo)
                    await s2.commit()
        asyncio.create_task(_bg())


def _kb(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записаться", callback_data=f"su:{tid}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"cx:{tid}"),
    ], [
        InlineKeyboardButton(text="👤 Записать гостя", callback_data=f"gu:{tid}"),
    ]])


class NewTraining(StatesGroup):
    title = State(); when = State(); location = State()
    duration = State(); maxp = State(); pubmode = State(); publish_at = State()


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
        svc = BookingService(session, tid)
        await _upsert_user(svc, message.from_user)
        await svc.repo.set_subscription(PLATFORM, message.from_user.id, True)
        await session.commit()
    text = ("Привет! Бот записи на тренировки. 🏸\n/list — тренировки и запись\n"
            "/profile — моя статистика")
    if features.statistics:
        text += "\n/stats — рейтинг и график"
    if is_admin:
        admin = ["\n\nАдмин:\n/new — создать\n/drafts — запустить черновик",
                 "/attend — явка и оплата\n/guests — подтвердить гостей",
                 "/setmax — лимит\n/cancel — отменить\n/broadcast — рассылка"]
        if features.statistics:
            admin.append("/debtors — должники")
        if features.exports:
            admin.append("/export — выгрузить список")
        text += "\n" + "\n".join(admin)
    await message.answer(text)


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    async with SessionLocal() as session:
        tid, _ = await _resolve_tenant(session, message.chat.id, message.from_user.id)
        if tid is None:
            await message.answer("Чат не привязан к клубу."); return
        svc = BookingService(session, tid)
        trainings = await svc.repo.list_upcoming()
        if not trainings:
            await message.answer("Ближайших тренировок нет."); return
        for t in trainings:
            await message.answer(await views.training_card(svc, t), reply_markup=_kb(t.id))


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
        tid, _ = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None:
            await query.answer("Чат не привязан к клубу.", show_alert=True); return
        svc = BookingService(session, tid)
        await _upsert_user(svc, query.from_user)
        res = await svc.sign_up(train_id, PLATFORM, query.from_user.id,
                                _name(query), username=_username(query))
        training = await svc.repo.get_training(train_id)
    await query.answer(views.signup_result(res, training.title if training else ""), show_alert=True)


@router.callback_query(F.data.startswith("cx:"))
async def cb_cancel(query: CallbackQuery) -> None:
    train_id = int(query.data.split(":")[1])
    async with SessionLocal() as session:
        tid, _ = await _resolve_tenant(session, query.message.chat.id, query.from_user.id)
        if tid is None:
            await query.answer("Чат не привязан к клубу.", show_alert=True); return
        g = GlobalRepository(session)
        tenant = await g.get_tenant(tid)
        lock = tenant.cancel_lock_minutes if tenant else 0
        svc = BookingService(session, tid)
        res = await svc.cancel_signup(train_id, PLATFORM, query.from_user.id,
                                      lock_minutes=lock)
    if res.get("locked"):
        await query.answer(
            f"Отмена закрыта: до тренировки меньше {res['lock_minutes']} мин. "
            f"Свяжитесь с тренером.", show_alert=True)
        return
    await query.answer("Запись отменена." if res["cancelled"] else "Вы не были записаны.", show_alert=True)


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
    await state.set_state(NewTraining.when)
    await message.answer("Дата и время: ДД.ММ.ГГГГ ЧЧ:ММ (напр. 20.06.2026 19:00)")


@router.message(NewTraining.when)
async def new_when(message: Message, state: FSMContext) -> None:
    parsed = BookingService(None, 0).parse_local(message.text)
    if not parsed:
        await message.answer("Неверный формат. Пример: 20.06.2026 19:00"); return
    await state.update_data(start_at=parsed.isoformat())
    await state.set_state(NewTraining.location)
    await message.answer("Место? (или «-»)")


@router.message(NewTraining.location)
async def new_location(message: Message, state: FSMContext) -> None:
    loc = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(location=loc)
    await state.set_state(NewTraining.duration)
    await message.answer("Длительность в минутах? (напр. 120)")


@router.message(NewTraining.duration)
async def new_duration(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите число минут, напр. 120."); return
    await state.update_data(duration=int(message.text))
    await state.set_state(NewTraining.maxp)
    await message.answer("Максимум участников?")


@router.message(NewTraining.maxp)
async def new_maxp(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите положительное число."); return
    await state.update_data(maxp=int(message.text))
    await state.set_state(NewTraining.pubmode)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Открыть сразу", callback_data="pm:now")],
        [InlineKeyboardButton(text="📝 Черновик", callback_data="pm:draft")],
        [InlineKeyboardButton(text="⏰ По таймеру", callback_data="pm:timer")]])
    await message.answer("Когда открыть запись?", reply_markup=kb)


async def _finalize(state: FSMContext, st: str, publish_at):
    data = await state.get_data()
    async with SessionLocal() as session:
        svc = BookingService(session, data["tenant_id"])
        training = await svc.create_training(
            title=data["title"], start_at=dt.datetime.fromisoformat(data["start_at"]),
            location=data["location"], max_participants=data["maxp"],
            duration_min=data["duration"], state=st, publish_at=publish_at,
            platform=PLATFORM, user_id=0)
        card = await views.training_card(svc, training)
    await state.clear()
    return card


@router.callback_query(NewTraining.pubmode, F.data.startswith("pm:"))
async def new_pubmode(query: CallbackQuery, state: FSMContext) -> None:
    mode = query.data.split(":")[1]
    await query.answer()
    if mode == "now":
        card = await _finalize(state, "published", None)
        await query.message.edit_text("Создано, запись открыта:")
        await query.message.answer(card)
    elif mode == "draft":
        card = await _finalize(state, "draft", None)
        await query.message.edit_text("Черновик создан (/drafts чтобы запустить):")
        await query.message.answer(card)
    else:
        await state.set_state(NewTraining.publish_at)
        await query.message.edit_text("Во сколько открыть запись? ДД.ММ.ГГГГ ЧЧ:ММ")


@router.message(NewTraining.publish_at)
async def new_publish_at(message: Message, state: FSMContext) -> None:
    parsed = BookingService(None, 0).parse_local(message.text)
    if not parsed:
        await message.answer("Неверный формат. Пример: 19.06.2026 09:00"); return
    card = await _finalize(state, "draft", parsed)
    await message.answer("Запланировано, запись откроется автоматически:")
    await message.answer(card)


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
    await query.message.answer(card)


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
