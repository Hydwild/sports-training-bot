"""
Автообновление карточки тренировки в TG-группе + видимость кнопки
«Обновить список» всем участникам.

Регресс: раньше кнопка обновления была видна только админу, а карточка в
группе обновлялась ТОЛЬКО при нажатии кнопки прямо на ней (cb_signup/
cb_cancel) — не при гостевой записи, подтверждении/отклонении гостя,
записи через веб-страницу или через VK. Участники видели устаревший
список записавшихся, пока кто-то не нажимал кнопку в самой группе.
"""
import datetime as dt

from fastapi.testclient import TestClient

import app.bots.telegram as tg
from app.main import app
from app.services.booking import BookingService

H = {"x-admin-token": "tok"}


def test_refresh_button_visible_for_participants_and_admin():
    kb_participant = tg._kb(1, is_admin=False)
    texts = [b.text for row in kb_participant.inline_keyboard for b in row]
    assert "🔄 Обновить список" in texts

    kb_admin = tg._kb(1, is_admin=True)
    texts_admin = [b.text for row in kb_admin.inline_keyboard for b in row]
    assert "🔄 Обновить список" in texts_admin


def _mk_tenant(admin_id: int, chat_id: int) -> int:
    """Клуб через HTTP API (TestClient) — заодно поднимает таблицы через
    lifespan, чтобы дальнейшая прямая работа с SessionLocal не падала на
    'no such table'. tg_chat_id обязателен и должен быть уникальным на
    тест: БД в тестах общая файловая (не изолированная), и без точного
    совпадения по chat_id _resolve_tenant() ищет клуб по admin_tg_id через
    fallback-цикл по ВСЕМ клубам — при повторяющемся admin_id в разных
    тестах это находит случайный, а не нужный клуб."""
    with TestClient(app) as c:
        return c.post("/api/tenants", json={
            "name": "Клуб Карточки", "admin_tg_id": admin_id, "tg_chat_id": chat_id,
        }, headers=H).json()["id"]


async def _mk_training(session_factory, tenant_id: int) -> int:
    svc = BookingService(session_factory, tenant_id)
    training = await svc.create_training(
        title="Тест", start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
        location="Зал", max_participants=5, platform="tg", user_id=0)
    await session_factory.commit()
    return training.id


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid
        self.username = None
        self.full_name = f"User{uid}"


class _FakeMessage:
    def __init__(self, text: str, user_id: int, chat_id: int = 555):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.chat = type("C", (), {"id": chat_id})()
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)


class _FakeState:
    def __init__(self, data: dict):
        self._data = data
        self.cleared = False

    async def get_data(self):
        return self._data

    async def clear(self):
        self.cleared = True


async def test_guest_signup_refreshes_group_card(monkeypatch):
    tid = _mk_tenant(admin_id=901, chat_id=1001)
    from app.db.engine import SessionLocal, engine
    await engine.dispose()
    async with SessionLocal() as s:
        train_id = await _mk_training(s, tid)

    refreshed = []

    async def fake_refresh(tenant_id, training_id):
        refreshed.append((tenant_id, training_id))

    monkeypatch.setattr(tg, "_refresh_group_card", fake_refresh)

    msg = _FakeMessage("Гость Иванов", user_id=100)
    state = _FakeState({"tenant_id": tid, "train_id": train_id})
    await tg.guest_name(msg, state)

    assert refreshed == [(tid, train_id)]
    assert any("записан" in t for t in msg.sent)
    assert state.cleared is True


async def test_guest_signup_closed_does_not_refresh_card(monkeypatch):
    """Если запись закрыта (тренировка отменена) — обновлять нечего."""
    tid = _mk_tenant(admin_id=902, chat_id=1002)
    from app.db.engine import SessionLocal, engine
    await engine.dispose()
    async with SessionLocal() as s:
        train_id = await _mk_training(s, tid)
        svc = BookingService(s, tid)
        training = await svc.repo.get_training(train_id)
        training.is_cancelled = True
        await s.commit()

    refreshed = []

    async def fake_refresh(tenant_id, training_id):
        refreshed.append((tenant_id, training_id))

    monkeypatch.setattr(tg, "_refresh_group_card", fake_refresh)

    msg = _FakeMessage("Гость Петров", user_id=100)
    state = _FakeState({"tenant_id": tid, "train_id": train_id})
    await tg.guest_name(msg, state)

    assert refreshed == []
    assert any("закрыта" in t or "отменена" in t for t in msg.sent)


class _FakeCallbackQuery:
    def __init__(self, data: str, user_id: int, chat_id: int = 555):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessageForCb(chat_id)
        self.answers: list[tuple] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


class _FakeMessageForCb:
    def __init__(self, chat_id: int):
        self.chat = type("C", (), {"id": chat_id})()
        self.edits: list[str] = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)


async def test_guest_confirm_refreshes_group_card(monkeypatch):
    admin_id, chat_id = 903, 1003
    tid = _mk_tenant(admin_id=admin_id, chat_id=chat_id)
    from app.db.engine import SessionLocal, engine
    await engine.dispose()
    async with SessionLocal() as s:
        train_id = await _mk_training(s, tid)
        svc = BookingService(s, tid)
        res = await svc.sign_up_guest(train_id, "Гость", added_by=100)
        assert res.result == "active"
        guest = (await svc.list_unconfirmed_guests(train_id))[0]
        sid = guest.id

    refreshed = []

    async def fake_refresh(tenant_id, training_id):
        refreshed.append((tenant_id, training_id))

    monkeypatch.setattr(tg, "_refresh_group_card", fake_refresh)

    query = _FakeCallbackQuery(f"gok:{sid}", user_id=admin_id, chat_id=chat_id)
    await tg.cb_guest_confirm(query)

    assert refreshed == [(tid, train_id)]
    assert any("подтверждён" in t for t in query.message.edits)


async def test_guest_reject_refreshes_group_card(monkeypatch):
    admin_id, chat_id = 904, 1004
    tid = _mk_tenant(admin_id=admin_id, chat_id=chat_id)
    from app.db.engine import SessionLocal, engine
    await engine.dispose()
    async with SessionLocal() as s:
        train_id = await _mk_training(s, tid)
        svc = BookingService(s, tid)
        res = await svc.sign_up_guest(train_id, "Гость", added_by=100)
        assert res.result == "active"
        guest = (await svc.list_unconfirmed_guests(train_id))[0]
        sid = guest.id

    refreshed = []

    async def fake_refresh(tenant_id, training_id):
        refreshed.append((tenant_id, training_id))

    monkeypatch.setattr(tg, "_refresh_group_card", fake_refresh)

    query = _FakeCallbackQuery(f"gno:{sid}", user_id=admin_id, chat_id=chat_id)
    await tg.cb_guest_reject(query)

    assert refreshed == [(tid, train_id)]
    assert any("отклонён" in t for t in query.message.edits)


# ---------- Кросс-платформенно: VK-запись/отмена обновляет TG-карточку ----------

async def test_vk_signup_refreshes_tg_group_card(monkeypatch):
    import app.bots.vk as vk

    vk_group = 20001
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "VK-TG клуб", "vk_group_id": vk_group,
        }, headers=H).json()["id"]

    from app.db.engine import SessionLocal, engine
    await engine.dispose()
    async with SessionLocal() as s:
        train_id = await _mk_training(s, tid)

    refreshed = []

    async def fake_refresh(tenant_id, training_id):
        refreshed.append((tenant_id, training_id))

    monkeypatch.setattr(vk, "_notify_tg_card_changed", fake_refresh)

    result = await vk._do_signup(user_id=777, tid=train_id, group_id=vk_group)
    assert "записаны" in result
    assert refreshed == [(tid, train_id)]


async def test_vk_cancel_refreshes_tg_group_card(monkeypatch):
    import app.bots.vk as vk

    vk_group = 20002
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "VK-TG клуб 2", "vk_group_id": vk_group,
        }, headers=H).json()["id"]

    from app.db.engine import SessionLocal, engine
    await engine.dispose()
    async with SessionLocal() as s:
        train_id = await _mk_training(s, tid)

    await vk._do_signup(user_id=778, tid=train_id, group_id=vk_group)

    refreshed = []

    async def fake_refresh(tenant_id, training_id):
        refreshed.append((tenant_id, training_id))

    monkeypatch.setattr(vk, "_notify_tg_card_changed", fake_refresh)

    result = await vk._do_cancel(user_id=778, tid=train_id, group_id=vk_group)
    assert result == "Запись отменена."
    assert refreshed == [(tid, train_id)]


# ---------- Публичная веб-запись/отмена обновляет TG-карточку ----------

def test_public_web_signup_refreshes_tg_group_card(monkeypatch):
    refreshed = []

    async def fake_refresh(tenant_id, training_id):
        refreshed.append((tenant_id, training_id))

    monkeypatch.setattr(tg, "_refresh_group_card", fake_refresh)

    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Веб-TG клуб"},
                     headers=H).json()["id"]
        train = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Игра",
            "start_at": (dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(days=1)).isoformat(),
            "max_participants": 5,
        }).json()
        train_id = train["id"]

        r = c.post(f"/club/{tid}/signup", data={"consent": "1", 
            "training_id": train_id, "name": "Веб Участник",
            "phone": "79991234567"})
        assert "Вы записаны" in r.text

    assert refreshed == [(tid, train_id)]


def test_public_web_cancel_refreshes_tg_group_card(monkeypatch):
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Веб-TG клуб 2"},
                     headers=H).json()["id"]
        train = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Игра",
            "start_at": (dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(days=1)).isoformat(),
            "max_participants": 5,
        }).json()
        train_id = train["id"]

        signup_resp = c.post(f"/club/{tid}/signup", data={"consent": "1", 
            "training_id": train_id, "name": "Веб Участник",
            "phone": "79991234568"})

        refreshed = []

        async def fake_refresh(tenant_id, training_id):
            refreshed.append((tenant_id, training_id))

        monkeypatch.setattr(tg, "_refresh_group_card", fake_refresh)

        import re
        link = re.search(r'href="(/club/\d+/cancel\?[^"]+)"',
                         signup_resp.text).group(1)
        link = link.replace("&amp;", "&")
        confirm = c.get(link)                      # только подтверждение
        fields = dict(re.findall(r'name="(\w+)" value="([^"]+)"', confirm.text))
        r = c.post(link.split("?")[0], data=fields)
        assert "отменена" in r.text

    assert refreshed == [(tid, train_id)]


# ---------- карточку правит бот ЭТОГО клуба ----------
#
# Telegram не даёт одному боту редактировать сообщения другого. Раньше
# обновление всегда шло глобальным ботом площадки: у клиента со своим
# ботом счётчик мест в закреплённой карточке замирал навсегда — и молча,
# потому что исключение гасилось целиком (`except Exception: pass`).
# Заметно это только у клуба со своим ботом И групповым чатом, поэтому на
# демо в личке дефект не проявлялся.

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.models.entities import Base, Tenant  # noqa: E402

CHAT_ID = -1001234567890


class _StubBot:
    """Заглушка: помнит, что и кем редактировалось."""

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.edits: list[tuple[int, int]] = []

    async def edit_message_text(self, text, chat_id=None, message_id=None,
                                **kw):
        if self.fail:
            raise self.fail
        self.edits.append((chat_id, message_id))


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


async def _club_with_card(maker, chat_id=CHAT_ID) -> tuple[int, int]:
    async with maker() as s:
        t = Tenant(name="Клуб со своим ботом", tg_chat_id=chat_id)
        s.add(t)
        await s.commit()
        tid = t.id
        svc = BookingService(s, tid)
        tr = await svc.repo.add_training(
            title="Занятие",
            start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
            location="Зал", max_participants=5, duration_min=60,
            state="published", publish_at=None,
            created_by_platform="test", created_by_id=0)
        tr.group_message_id = 777
        await s.commit()
        return tid, tr.id


async def test_clients_own_bot_edits_the_card(maker, monkeypatch):
    """Ключевой инвариант: правит бот клуба, а не глобальный."""
    tid, train_id = await _club_with_card(maker)
    platform_bot, club_bot = _StubBot(), _StubBot()
    monkeypatch.setattr(tg, "_bot", platform_bot)
    monkeypatch.setattr(tg, "_tenant_bots", {tid: club_bot})

    await tg._refresh_group_card(tid, train_id)

    assert club_bot.edits == [(CHAT_ID, 777)], "карточку правил не бот клуба"
    assert platform_bot.edits == [], "глобальный бот полез в чужой чат"


async def test_platform_bot_used_when_club_has_none(maker, monkeypatch):
    """Клуб без своего бота обслуживает бот площадки — как и раньше."""
    tid, train_id = await _club_with_card(maker)
    platform_bot = _StubBot()
    monkeypatch.setattr(tg, "_bot", platform_bot)
    monkeypatch.setattr(tg, "_tenant_bots", {})

    await tg._refresh_group_card(tid, train_id)
    assert platform_bot.edits == [(CHAT_ID, 777)]


async def test_no_bot_at_all_is_silent(maker, monkeypatch):
    tid, train_id = await _club_with_card(maker)
    monkeypatch.setattr(tg, "_bot", None)
    monkeypatch.setattr(tg, "_tenant_bots", {})
    await tg._refresh_group_card(tid, train_id)      # не падает


async def test_unchanged_card_is_not_logged(maker, monkeypatch, caplog):
    """«message is not modified» приходит постоянно — засорять лог нельзя."""
    tid, train_id = await _club_with_card(maker)
    bot = _StubBot(fail=RuntimeError("Bad Request: message is not modified"))
    monkeypatch.setattr(tg, "_bot", None)
    monkeypatch.setattr(tg, "_tenant_bots", {tid: bot})

    with caplog.at_level("WARNING"):
        await tg._refresh_group_card(tid, train_id)
    assert "не обновлена" not in caplog.text


async def test_real_failure_is_logged(maker, monkeypatch, caplog):
    """А «нет прав» или «сообщение удалено» замалчивать нельзя: именно так
    и жил незамеченным баг с чужим ботом."""
    tid, train_id = await _club_with_card(maker)
    bot = _StubBot(fail=RuntimeError("Forbidden: bot is not a member"))
    monkeypatch.setattr(tg, "_bot", None)
    monkeypatch.setattr(tg, "_tenant_bots", {tid: bot})

    with caplog.at_level("WARNING"):
        await tg._refresh_group_card(tid, train_id)
    assert "не обновлена" in caplog.text and str(tid) in caplog.text


@pytest.mark.parametrize("chat_id", [None, -100])
async def test_club_without_group_chat_is_skipped(maker, monkeypatch, chat_id):
    """Без группового чата обновлять нечего."""
    tid, train_id = await _club_with_card(maker, chat_id)
    bot = _StubBot()
    monkeypatch.setattr(tg, "_bot", None)
    monkeypatch.setattr(tg, "_tenant_bots", {tid: bot})
    await tg._refresh_group_card(tid, train_id)
    assert bot.edits == []
