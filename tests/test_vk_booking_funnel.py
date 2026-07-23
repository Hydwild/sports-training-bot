"""
Воронка записи в VK: день → время → занятие.

В Telegram человек сначала выбирает дату, потом время из свободных, и только
потом видит карточку со специалистом. В VK до этого был плоский список всех
занятий подряд — при десятке слотов это стена карточек.

Отдельная сложность VK: inline-клавиатура вмещает всего шесть кнопок. Значит
воронка обязана честно признаваться, когда показала не всё, а не молча
терять дни — иначе человек решит, что свободных мест нет.

Воронка включена только салонам и репетиторам. У спорт-клубов групповых
занятий немного, их важно видеть сразу все вместе со свободными местами —
там остаётся привычный плоский список.
"""
import datetime as dt
import json

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bots import vk
from app.models.entities import Base, Tenant
from app.services.booking import BookingService

GROUP_ID = 555


class _Messages:
    def __init__(self):
        self.sent, self.edited = [], []
        self._next_id = 1000

    async def send(self, user_id=None, message=None, keyboard=None, **kw):
        self._next_id += 1
        self.sent.append((user_id, message, keyboard))
        return self._next_id

    async def edit(self, peer_id=None, message=None, keyboard=None, **kw):
        self.edited.append((peer_id, message, keyboard))
        return 1


class _Api:
    def __init__(self):
        self.messages = _Messages()


@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(vk, "SessionLocal", m)
    yield m
    await engine.dispose()


@pytest_asyncio.fixture
async def api(monkeypatch):
    a = _Api()
    monkeypatch.setattr(vk, "_api", lambda: a)
    monkeypatch.setattr(vk, "_api_by_tenant", {})
    monkeypatch.setattr(vk, "_configured_tenants", set())
    return a


async def _club(maker, vertical: str = "beauty") -> int:
    async with maker() as s:
        t = Tenant(name="Клуб ВК", vk_group_id=GROUP_ID, vertical=vertical)
        s.add(t)
        await s.commit()
        return t.id


async def _slot(maker, tenant_id: int, when: dt.datetime, *,
                title="Занятие", cap=5) -> int:
    async with maker() as s:
        svc = BookingService(s, tenant_id)
        tr = await svc.repo.add_training(
            title=title, start_at=when, location="Зал",
            max_participants=cap, duration_min=60,
            state="published", publish_at=None,
            created_by_platform="test", created_by_id=0)
        await s.commit()
        return tr.id


def _buttons(keyboard_json: str) -> list[dict]:
    """Кнопки клавиатуры плоским списком: (label, payload, color)."""
    kb = json.loads(keyboard_json)
    out = []
    for row in kb["buttons"]:
        for btn in row:
            act = btn["action"]
            out.append({"label": act["label"],
                        "payload": json.loads(act["payload"]),
                        "color": btn.get("color")})
    return out


def _in_days(n: int) -> dt.datetime:
    """Полдень UTC: при любом часовом поясе клуба слот остаётся в этом дне,
    иначе тест ломался бы в зависимости от времени суток на машине."""
    day = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=n)).date()
    return dt.datetime.combine(day, dt.time(9, 0), dt.timezone.utc)


async def _first_day(api) -> str:
    """Дату спрашиваем у самой воронки: она считает день по поясу клуба,
    а не по UTC."""
    await vk._vk_open_funnel(777, GROUP_ID)
    day = _buttons(api.messages.sent[-1][2])[0]["payload"]["d"]
    api.messages.sent.clear()
    return day


# ---------- шаг 1: дни ----------

async def test_funnel_offers_days_not_a_wall_of_cards(maker, api):
    """Главное отличие от старого поведения: одно сообщение с датами
    вместо карточки на каждое занятие."""
    tid = await _club(maker)
    for n in (1, 1, 2):
        await _slot(maker, tid, _in_days(n))

    await vk._vk_open_funnel(777, GROUP_ID)

    assert len(api.messages.sent) == 1
    _, text, kb = api.messages.sent[0]
    assert "Выберите день" in text
    btns = _buttons(kb)
    assert len(btns) == 2                       # два дня, а не три занятия
    assert [b["payload"]["a"] for b in btns] == ["bd", "bd"]
    assert all("d" in b["payload"] for b in btns)


async def test_full_day_is_shown_but_marked(maker, api):
    """День без мест не прячем: расписание должно быть видно целиком,
    иначе человек думает, что мы просто не работаем в этот день."""
    tid = await _club(maker)
    train_id = await _slot(maker, tid, _in_days(1), cap=1)
    async with maker() as s:
        svc = BookingService(s, tid)
        await svc.sign_up(train_id, "test", 1, "Кто-то")
        await s.commit()

    await vk._vk_open_funnel(777, GROUP_ID)

    (btn,) = _buttons(api.messages.sent[0][2])
    assert btn["label"].endswith("·")


async def test_extra_days_are_declared_not_dropped(maker, api):
    """VK даёт шесть кнопок. Седьмой день молча исчезнуть не может —
    об этом сказано в тексте."""
    tid = await _club(maker)
    for n in range(1, 9):
        await _slot(maker, tid, _in_days(n))

    await vk._vk_open_funnel(777, GROUP_ID)

    _, text, kb = api.messages.sent[0]
    assert len(_buttons(kb)) == vk.VK_INLINE_LIMIT
    assert "первые 6 из 8" in text


async def test_no_slots_says_so(maker, api):
    await _club(maker)
    await vk._vk_open_funnel(777, GROUP_ID)
    assert len(api.messages.sent) == 1
    assert api.messages.sent[0][2] is None      # без клавиатуры выбора


# ---------- шаг 2: время ----------

async def test_day_step_edits_the_same_message(maker, api):
    """Каждый шаг правит одно сообщение. Иначе диалог за три клика
    превращается в простыню."""
    tid = await _club(maker)
    await _slot(maker, tid, _in_days(1))
    day = await _first_day(api)

    await vk._vk_show_day(peer_id=777, cmid=5, group_id=GROUP_ID, raw=day)

    assert api.messages.sent == []
    assert len(api.messages.edited) == 1
    _, text, kb = api.messages.edited[0]
    assert "выберите время" in text
    labels = [b["payload"]["a"] for b in _buttons(kb)]
    assert labels == ["bt", "bd"]              # время + «← К датам»


async def test_times_leave_room_for_the_back_button(maker, api):
    """Пять времён плюс «назад» — ровно шесть, предел VK."""
    tid = await _club(maker)
    base = _in_days(1)
    for h in range(8):
        await _slot(maker, tid, base + dt.timedelta(minutes=10 * h),
                    title=f"Слот {h}")
    day = await _first_day(api)

    await vk._vk_show_day(peer_id=777, cmid=5, group_id=GROUP_ID, raw=day)

    _, text, kb = api.messages.edited[0]
    btns = _buttons(kb)
    assert len(btns) == vk.VK_INLINE_LIMIT
    assert btns[-1]["payload"] == {"a": "bd", "d": "back"}
    assert "первые 5 из 8" in text


async def test_back_returns_to_days(maker, api):
    tid = await _club(maker)
    await _slot(maker, tid, _in_days(1))

    await vk._vk_show_day(peer_id=777, cmid=5, group_id=GROUP_ID, raw="back")

    _, text, kb = api.messages.edited[0]
    assert "Выберите день" in text
    assert [b["payload"]["a"] for b in _buttons(kb)] == ["bd"]


async def test_broken_date_does_not_edit_anything(maker, api):
    tid = await _club(maker)
    await _slot(maker, tid, _in_days(1))

    answer = await vk._vk_show_day(peer_id=777, cmid=5, group_id=GROUP_ID,
                                   raw="не-дата")

    assert api.messages.edited == []
    assert "дату" in answer


# ---------- шаг 3: занятие ----------

async def test_slot_step_shows_card_with_signup(maker, api):
    """Специалист виден только на третьем шаге — как в Telegram."""
    tid = await _club(maker)
    train_id = await _slot(maker, tid, _in_days(1), title="Йога")

    await vk._vk_show_slot(peer_id=777, cmid=5, group_id=GROUP_ID,
                           tid=train_id)

    _, text, kb = api.messages.edited[0]
    assert "Йога" in text
    payloads = [b["payload"] for b in _buttons(kb)]
    assert {"a": "su", "tid": train_id} in payloads


async def test_vanished_slot_is_reported_not_crashed(maker, api):
    """Занятие могли удалить между шагами — кнопка не должна ронять бота."""
    await _club(maker)
    answer = await vk._vk_show_slot(peer_id=777, cmid=5, group_id=GROUP_ID,
                                    tid=99999)
    assert api.messages.edited == []
    assert "недоступна" in answer


# ---------- развилка по вертикали ----------

async def test_salon_menu_opens_the_funnel(maker, api):
    tid = await _club(maker, "beauty")
    await _slot(maker, tid, _in_days(1))

    await vk._open_booking(777, GROUP_ID)

    (_, text, kb) = api.messages.sent[0]
    assert "Выберите день" in text
    assert [b["payload"]["a"] for b in _buttons(kb)] == ["bd"]


async def test_tutor_menu_opens_the_funnel(maker, api):
    tid = await _club(maker, "tutor")
    await _slot(maker, tid, _in_days(1))

    await vk._open_booking(777, GROUP_ID)

    assert "Выберите день" in api.messages.sent[0][1]


async def test_sport_menu_keeps_the_flat_list(maker, api):
    """Тренировкам воронку не навязываем: карточки со свободными местами
    видны сразу, лишний шаг только мешает."""
    tid = await _club(maker, "sport")
    for n in (1, 2):
        await _slot(maker, tid, _in_days(n))

    await vk._open_booking(777, GROUP_ID)

    texts = [m[1] for m in api.messages.sent]
    assert not any("Выберите день" in t for t in texts)
    # две карточки занятий плюс напоминание про меню
    assert len(texts) == 3
    assert any(b["payload"] == {"a": "su", "tid": 1}
               for b in _buttons(api.messages.sent[0][2]))


async def test_club_without_vertical_is_sport(maker, api):
    """Все существующие клубы заведены без вертикали — их поведение
    меняться не должно."""
    tid = await _club(maker, None)
    await _slot(maker, tid, _in_days(1))

    await vk._open_booking(777, GROUP_ID)

    assert not any("Выберите день" in m[1] for m in api.messages.sent)
