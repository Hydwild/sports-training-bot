"""
Воронка записи в боте: день → время → специалист.

Плоский список карточек годится, пока слотов пять. У салона или репетитора
окон на неделю десятки, и листать их в чате невозможно — на странице
записи такая воронка уже была, теперь она есть и в боте.

Отдельно проверяем, что день БЕЗ свободных мест не исчезает из списка:
скрытый день читается как «клуб в этот день не работает», хотя на самом
деле туда можно встать в очередь.
"""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bots import telegram as tg
from app.models.entities import Base, Master, Tenant
from app.services.booking import BookingService


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tg, "SessionLocal", m)
    yield m
    await engine.dispose()


async def _club(maker, vertical="beauty") -> int:
    async with maker() as s:
        t = Tenant(name="Салон", vertical=vertical, timezone="Europe/Moscow")
        s.add(t)
        await s.commit()
        return t.id


async def _slot(maker, tid, *, days: int, hour: int, title: str,
                seats: int = 2, master_id=None, taken: int = 0):
    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        start = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(days=days)).replace(
                     hour=hour, minute=0, second=0, microsecond=0)
        t = await svc.repo.add_training(
            title=title, start_at=start, location="Кабинет",
            max_participants=seats, duration_min=60, state="published",
            publish_at=None, created_by_platform="test", created_by_id=0,
            master_id=master_id)
        await s.commit()
        for i in range(taken):
            await svc.sign_up(t.id, "demo", 900500 + i, f"Гость {i}")
        await s.commit()
        return t.id


async def _svc(maker, tid):
    async with maker() as s:
        yield BookingService(s, tid, tz="Europe/Moscow")


# ---------- шаг 1: дни ----------

async def test_days_are_grouped_and_sorted(maker):
    tid = await _club(maker)
    await _slot(maker, tid, days=3, hour=10, title="Стрижка")
    await _slot(maker, tid, days=1, hour=12, title="Маникюр")
    await _slot(maker, tid, days=1, hour=15, title="Окрашивание")

    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        by_day = await tg._slots_by_day(svc)

    assert len(by_day) == 2, "слоты одного дня должны схлопнуться в один день"
    days = list(by_day)
    assert days == sorted(days), "дни не отсортированы"
    first = by_day[days[0]]
    assert [x.title for x in first] == ["Маникюр", "Окрашивание"], \
        "время внутри дня не по возрастанию"


async def test_day_without_free_seats_is_still_shown(maker):
    """Скрытый день читается как «клуб не работает», хотя туда можно
    встать в очередь."""
    tid = await _club(maker)
    await _slot(maker, tid, days=2, hour=11, title="Занято", seats=1, taken=1)

    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        days = await tg._days_with_free(svc)

    assert len(days) == 1
    day, free = days[0]
    assert free == 0
    assert "·" in tg._day_label(day, free), "день без мест ничем не помечен"


async def test_free_places_never_negative(maker):
    """Сверх лимита — это очередь, она считается отдельно."""
    tid = await _club(maker)
    sid = await _slot(maker, tid, days=1, hour=9, title="Полный",
                      seats=1, taken=3)
    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        t = await svc.repo.get_training(sid)
        assert await tg._free_places(svc, t) == 0


async def test_days_keyboard_rows_are_three_wide(maker):
    tid = await _club(maker)
    for d in range(1, 8):
        await _slot(maker, tid, days=d, hour=10, title=f"Слот {d}")
    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        days = await tg._days_with_free(svc)
    kb = tg._days_kb(days)
    assert all(len(row) <= 3 for row in kb.inline_keyboard)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all(d.startswith("bd:") for d in data)
    assert len(data) == 7


# ---------- шаг 2: время ----------

async def test_times_show_free_seats_and_queue(maker):
    tid = await _club(maker)
    await _slot(maker, tid, days=1, hour=10, title="Есть места", seats=3)
    await _slot(maker, tid, days=1, hour=14, title="Занято", seats=1, taken=1)

    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        by_day = await tg._slots_by_day(svc)
        slots = by_day[list(by_day)[0]]
        kb = await tg._times_kb(svc, slots)

    labels = [b.text for row in kb.inline_keyboard for b in row]
    slots_labels = [x for x in labels if "К датам" not in x]
    assert len(slots_labels) == 2, slots_labels
    # к конкретным часам не привязываемся: они зависят от часового пояса
    import re
    assert all(re.match(r"^\d{2}:\d{2} ", x) for x in slots_labels), slots_labels
    assert any("Есть места" in x and "св." in x for x in slots_labels), slots_labels
    assert any("Занято" in x and "очередь" in x for x in slots_labels), slots_labels
    # возврат к датам обязателен: иначе из шага 2 нет выхода
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "bd:back" in data


async def test_time_buttons_fit_telegram_limit(maker):
    """Telegram обрезает подпись кнопки — длинное название не должно
    ломать вёрстку."""
    tid = await _club(maker)
    await _slot(maker, tid, days=1, hour=10,
                title="Очень длинное название услуги " * 5)
    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        by_day = await tg._slots_by_day(svc)
        kb = await tg._times_kb(svc, by_day[list(by_day)[0]])
    for row in kb.inline_keyboard:
        for b in row:
            assert len(b.text) <= 64, len(b.text)


# ---------- шаг 3: специалист ----------

async def test_card_shows_the_specialist(maker):
    """Ради этого шага воронка и делалась: мастера видно после выбора
    времени, а не до."""
    from app.bots import views

    tid = await _club(maker)
    async with maker() as s:
        m = Master(tenant_id=tid, name="Марина Ковалёва",
                   specialty="Парикмахер", active=True)
        s.add(m)
        await s.commit()
        master_id = m.id

    sid = await _slot(maker, tid, days=1, hour=10, title="Стрижка",
                      master_id=master_id)
    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        t = await svc.repo.get_training(sid)
        card = await views.training_card(svc, t)

    assert "Марина Ковалёва" in card
    assert "Мастер" in card, "в салоне специалист должен называться мастером"


@pytest.mark.parametrize("vertical,word", [
    ("sport", "Тренер"), ("beauty", "Мастер"), ("tutor", "Преподаватель"),
])
async def test_specialist_word_follows_vertical(maker, vertical, word):
    from app.bots import views

    tid = await _club(maker, vertical)
    async with maker() as s:
        m = Master(tenant_id=tid, name="Специалист", active=True)
        s.add(m)
        await s.commit()
        master_id = m.id
    sid = await _slot(maker, tid, days=1, hour=10, title="Слот",
                      master_id=master_id)
    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        card = await views.training_card(svc, await svc.repo.get_training(sid))
    assert word in card, card


# ---------- пустой случай ----------

async def test_no_slots_gives_vertical_specific_message(maker):
    tid = await _club(maker, "tutor")
    async with maker() as s:
        svc = BookingService(s, tid, tz="Europe/Moscow")
        assert await tg._days_with_free(svc) == []

    from app.core.verticals import vcfg
    assert "занятий" in vcfg("tutor")["web_empty"]


# ---------- воронка включена не всем ----------

class _Msg:
    """Минимум от aiogram.Message, который трогает btn_list."""

    class _Chat:
        id, type = 500, "private"

    class _User:
        id, full_name, username = 42, "Гость", None

    def __init__(self):
        self.chat, self.from_user = self._Chat(), self._User()
        self.answers: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


async def _press_list(maker, monkeypatch, vertical) -> _Msg:
    tid = await _club(maker, vertical)
    await _slot(maker, tid, days=1, hour=10, title="Окно")
    token = tg._ctx_tenant.set(tid)
    try:
        msg = _Msg()
        await tg.btn_list(msg)
        return msg
    finally:
        tg._ctx_tenant.reset(token)


@pytest.mark.parametrize("vertical", ["beauty", "tutor"])
async def test_menu_opens_funnel_for_salons_and_tutors(maker, monkeypatch,
                                                       vertical):
    msg = await _press_list(maker, monkeypatch, vertical)
    assert [t for t, _ in msg.answers] == ["📅 Выберите день:"]


@pytest.mark.parametrize("vertical", ["sport", None])
async def test_menu_keeps_flat_list_for_sport(maker, monkeypatch, vertical):
    """Тренировкам воронка не нужна: групповых занятий немного, и свободные
    места важно видеть сразу, без лишнего шага. None — существующие клубы,
    заведённые до вертикалей: их поведение меняться не должно."""
    msg = await _press_list(maker, monkeypatch, vertical)
    assert all("Выберите день" not in t for t, _ in msg.answers)
    assert "Окно" in msg.answers[0][0]
