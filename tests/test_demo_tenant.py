"""
Демо-клуб (Tenant.is_demo=True): любой написавший боту /start выбирает роль
«тренер» (Membership role=coach) или «участник» — без этого обычные клубы
(is_demo=False) не должны получить ни грамма новой логики, поэтому здесь же
регресс-проверки на обычный клуб.
"""
from fastapi.testclient import TestClient

import app.bots.telegram as tg
from app.main import app
from app.db.engine import SessionLocal, engine
from app.repositories.repo import GlobalRepository, TenantRepository

H = {"x-admin-token": "tok"}


def _mk_tenant(name: str, admin_id: int, chat_id: int, is_demo: bool = False) -> int:
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": name, "admin_tg_id": admin_id, "tg_chat_id": chat_id,
        }, headers=H).json()["id"]
    return tid


async def _set_demo(tid: int, is_demo: bool = True) -> None:
    await engine.dispose()
    async with SessionLocal() as s:
        t = await GlobalRepository(s).get_tenant(tid)
        t.is_demo = is_demo
        await s.commit()


class _FakeUser:
    def __init__(self, uid: int):
        self.id = uid
        self.username = None
        self.full_name = f"User{uid}"


class _FakeMessage:
    def __init__(self, text: str, user_id: int, chat_id: int):
        self.text = text
        self.from_user = _FakeUser(user_id)
        self.chat = type("C", (), {"id": chat_id, "type": "private"})()
        self.sent: list[tuple] = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


class _FakeCallbackQuery:
    def __init__(self, data: str, user_id: int, chat_id: int):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeMessageForCb(chat_id)
        self.answers: list[tuple] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


class _FakeMessageForCb:
    def __init__(self, chat_id: int):
        self.chat = type("C", (), {"id": chat_id, "type": "private"})()
        self.edits: list[str] = []
        self.sent: list[tuple] = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


# ---------- _is_admin_for: демо не влияет на обычные клубы ----------

async def test_regular_tenant_admin_only_by_admin_tg_id():
    tid = _mk_tenant("Обычный клуб", admin_id=501, chat_id=6001)
    await engine.dispose()
    async with SessionLocal() as s:
        t = await GlobalRepository(s).get_tenant(tid)
        assert await tg._is_admin_for(s, t, 501) is True
        assert await tg._is_admin_for(s, t, 999) is False
        # Membership с coach на НЕ-демо клубе не должен давать бот-админку
        await TenantRepository(s, tid).upsert_membership(999, "coach", "X")
        await s.commit()
    async with SessionLocal() as s:
        t = await GlobalRepository(s).get_tenant(tid)
        assert await tg._is_admin_for(s, t, 999) is False


async def test_demo_tenant_membership_grants_admin():
    tid = _mk_tenant("Демо клуб", admin_id=502, chat_id=6002)
    await _set_demo(tid)
    async with SessionLocal() as s:
        t = await GlobalRepository(s).get_tenant(tid)
        assert await tg._is_admin_for(s, t, 777) is False  # ещё не выбрал роль
        await TenantRepository(s, tid).upsert_membership(777, "coach", "Демо-тренер")
        await s.commit()
    async with SessionLocal() as s:
        t = await GlobalRepository(s).get_tenant(tid)
        assert await tg._is_admin_for(s, t, 777) is True
        # роль "assistant" не даёт бот-админку даже на демо
        await TenantRepository(s, tid).upsert_membership(778, "assistant", "X")
        await s.commit()
    async with SessionLocal() as s:
        t = await GlobalRepository(s).get_tenant(tid)
        assert await tg._is_admin_for(s, t, 778) is False


# ---------- cmd_start: выбор роли только на демо и только один раз ----------

async def test_start_shows_role_picker_on_demo_for_new_visitor():
    tid = _mk_tenant("Демо для старта", admin_id=503, chat_id=6003)
    await _set_demo(tid)
    msg = _FakeMessage("/start", user_id=1001, chat_id=6003)
    await tg.cmd_start(msg)
    assert len(msg.sent) == 1
    text, kw = msg.sent[0]
    assert "Демо-версия" in text
    kb = kw["reply_markup"]
    # кнопки выбора роли на месте; рядом может быть ссылка на витрину
    # (её нет, если PUBLIC_BASE_URL не настроен — см. test_bot_site_link)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row
             if b.callback_data]
    assert datas == ["demo:coach", "demo:participant"]


async def test_start_skips_picker_for_admin_and_regular_club():
    # обычный клуб — как раньше, без пикера
    _mk_tenant("Обычный для старта", admin_id=504, chat_id=6004)
    msg = _FakeMessage("/start", user_id=2001, chat_id=6004)
    await tg.cmd_start(msg)
    assert all("Демо-версия" not in t for t, _ in msg.sent)

    # сам админ демо-клуба тоже не должен видеть пикер
    tid2 = _mk_tenant("Демо-админ", admin_id=505, chat_id=6005)
    await _set_demo(tid2)
    msg2 = _FakeMessage("/start", user_id=505, chat_id=6005)
    await tg.cmd_start(msg2)
    assert all("Демо-версия" not in t for t, _ in msg2.sent)


async def test_start_skips_picker_after_role_already_chosen():
    tid = _mk_tenant("Демо повтор", admin_id=506, chat_id=6006)
    await _set_demo(tid)
    async with SessionLocal() as s:
        await TenantRepository(s, tid).upsert_membership(3001, "coach", "Уже тренер")
        await s.commit()
    msg = _FakeMessage("/start", user_id=3001, chat_id=6006)
    await tg.cmd_start(msg)
    assert all("Демо-версия" not in t for t, _ in msg.sent)


# ---------- cb_demo_role: выбор роли создаёт/не создаёт Membership ----------

async def test_cb_demo_coach_creates_membership_and_shows_admin_menu():
    tid = _mk_tenant("Демо коллбек", admin_id=507, chat_id=6007)
    await _set_demo(tid)
    query = _FakeCallbackQuery("demo:coach", user_id=4001, chat_id=6007)
    await tg.cb_demo_role(query)

    async with SessionLocal() as s:
        m = await TenantRepository(s, tid).get_membership(4001)
        assert m is not None and m.role == "coach"
    assert any("Вы тренер" in t for t in query.message.edits)
    # меню тренера + приглашение открыть витрину (второе появляется только
    # при настроенном PUBLIC_BASE_URL — см. test_bot_site_link)
    assert query.message.sent


async def test_cb_demo_participant_does_not_create_membership():
    tid = _mk_tenant("Демо коллбек-участник", admin_id=508, chat_id=6008)
    await _set_demo(tid)
    query = _FakeCallbackQuery("demo:participant", user_id=4002, chat_id=6008)
    await tg.cb_demo_role(query)

    async with SessionLocal() as s:
        m = await TenantRepository(s, tid).get_membership(4002)
        assert m is None
    assert any("Вы участник" in t for t in query.message.edits)
