"""
Вертикали бизнеса (sport/beauty) и мастера: терминология страницы записи,
API мастеров, карточка мастера на странице, лента дней, индивидуальные
слоты (max_participants=1), вертикаль в сборке конструктора.
"""
import datetime as dt
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    yield
    api_routes._ip_hits.clear()


def _mk_training(c, tid, title="Слот", days=3, hour_shift=0, maxp=2, **extra):
    start = (dt.datetime.now(dt.timezone.utc)
             + dt.timedelta(days=days, hours=hour_shift)).isoformat()
    r = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": title, "start_at": start, "max_participants": maxp, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_beauty_vertical_changes_page_texts():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Бритва", "vertical": "beauty"}, headers=H).json()["id"]
        page = c.get(f"/club/{tid}").text
        assert "Онлайн-запись" in page               # eyebrow вертикали
        assert "Свободных окон пока нет" in page     # пустое состояние beauty


def test_sport_vertical_stays_default():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Спорт"},
                     headers=H).json()["id"]
        page = c.get(f"/club/{tid}").text
        assert "Запись на тренировки" in page
        assert "Ближайших тренировок нет" in page


def test_unknown_vertical_falls_back_to_sport():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Клуб X", "vertical": "nonsense"}, headers=H).json()["id"]
        assert "Запись на тренировки" in c.get(f"/club/{tid}").text


def test_masters_api_and_page_card():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Мастеров", "vertical": "beauty"},
            headers=H).json()["id"]
        # API защищён
        assert c.get(f"/api/tenants/{tid}/masters").status_code == 401
        # javascript: в фото отклоняется
        assert c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Злой", "photo_url": "javascript:alert(1)"}).status_code == 422
        # мастер без фото
        m = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Анна Барберова", "specialty": "Стрижки и укладки"}).json()
        # слот с мастером
        _mk_training(c, tid, title="Стрижка", maxp=1, master_id=m["id"])
        page = c.get(f"/club/{tid}").text
        assert "Анна Барберова" in page
        assert "Стрижки и укладки" in page
        assert 'class="mi">А<' in page          # инициал вместо фото
        assert "время свободно" in page         # индивидуальный слот, не бар
        # несуществующий мастер у слота — 400
        r = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "X", "start_at": (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(days=1)).isoformat(),
            "max_participants": 1, "master_id": 99999})
        assert r.status_code == 400


def test_master_photo_rendered_when_set():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Фото", "vertical": "beauty"}, headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Ольга", "photo_url": "https://example.com/olga.jpg"}).json()
        _mk_training(c, tid, title="Маникюр", maxp=1, master_id=m["id"])
        page = c.get(f"/club/{tid}").text
        assert 'src="https://example.com/olga.jpg"' in page


def test_single_slot_full_shows_waitlist_text():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Очередь", "vertical": "beauty"},
            headers=H).json()["id"]
        tr = _mk_training(c, tid, title="Депиляция", maxp=1)
        c.post(f"/club/{tid}/signup", data={
            "training_id": tr, "name": "Ирина", "phone": "79990001122"})
        page = c.get(f"/club/{tid}").text
        assert "время занято — запись в лист ожидания" in page


def test_days_strip_appears_for_multiple_days():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Дней"},
                     headers=H).json()["id"]
        _mk_training(c, tid, title="День1", days=2)
        _mk_training(c, tid, title="День2", days=3)
        page = c.get(f"/club/{tid}").text
        assert 'class="days"' in page
        assert page.count('class="day"') == 2
        assert re.search(r'<div class="card" data-day="\d{4}-\d{2}-\d{2}"', page)
        assert 'id="list"' in page


def test_days_strip_hidden_for_single_day():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Одного Дня"},
                     headers=H).json()["id"]
        _mk_training(c, tid, title="Единственная", days=2)
        page = c.get(f"/club/{tid}").text
        assert 'class="days"' not in page       # один день — лента не нужна
        assert "Единственная" in page


def _menu_texts(kb) -> list[str]:
    return [b.text for row in kb.keyboard for b in row]


def test_tg_menu_sport_unchanged():
    """Регресс: у sport-клубов тексты кнопок byte-в-byte исторические —
    существующие клубы не должны заметить появления вертикалей."""
    import app.bots.telegram as tg
    texts = _menu_texts(tg._menu(True, vertical="sport"))
    assert "➕ Создать тренировку" in texts
    assert "🏸 Тренировки" in texts
    assert "✅ Явки" in texts
    assert "👤 Записать гостя" in texts
    p_texts = _menu_texts(tg._menu(False, vertical="sport"))
    assert "🏆 Рейтинг" in p_texts


def test_tg_menu_beauty_buttons():
    import app.bots.telegram as tg
    texts = _menu_texts(tg._menu(True, vertical="beauty"))
    assert "➕ Добавить время" in texts
    assert "📅 Записаться" in texts
    assert "✅ Визиты" in texts
    assert "👤 Записать клиента" in texts
    assert "🏸 Тренировки" not in texts
    p_texts = _menu_texts(tg._menu(False, vertical="beauty"))
    assert "🏆 Рейтинг" not in p_texts   # рейтинг посещаемости — не для салона


def test_tg_handlers_match_both_vertical_texts():
    import app.bots.telegram as tg
    assert {"🏸 Тренировки", "📅 Записаться"} <= tg._BTN_LIST_ALL
    assert {"➕ Создать тренировку", "➕ Добавить время"} <= tg._BTN_NEW_ALL
    assert {"✅ Явки", "✅ Визиты"} <= tg._BTN_ATTEND_ALL
    assert {"👤 Записать гостя", "👤 Записать клиента"} <= tg._BTN_GUESTS_ALL


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.username = None
        self.full_name = f"U{uid}"


class _FakeMessage:
    def __init__(self, user_id, chat_id):
        self.text = "/start"
        self.from_user = _FakeUser(user_id)
        self.chat = type("C", (), {"id": chat_id, "type": "private"})()
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


async def test_cmd_start_beauty_welcome_and_menu():
    import app.bots.telegram as tg
    from app.db.engine import engine
    with TestClient(app) as c:
        c.post("/api/tenants", json={
            "name": "Салон Старт", "vertical": "beauty",
            "admin_tg_id": 7501, "tg_chat_id": 87501}, headers=H)
    await engine.dispose()
    msg = _FakeMessage(user_id=7501, chat_id=87501)
    await tg.cmd_start(msg)
    text, kw = msg.sent[-1]
    assert "онлайн-записи" in text
    texts = _menu_texts(kw["reply_markup"])
    assert "➕ Добавить время" in texts


def test_vk_menu_beauty_labels_and_sport_default():
    import json
    import app.bots.vk as vk

    def labels(kb_json):
        kb = json.loads(kb_json)
        return [b["action"]["label"] for row in kb["buttons"] for b in row]

    token = vk._ctx_vertical.set("beauty")
    try:
        texts = labels(vk._menu_kb(True))
        assert "➕ Добавить время" in texts
        assert "📅 Записаться" in texts
        assert "✅ Визиты" in texts
        assert "👤 Записать клиента" in texts
        p_texts = labels(vk._menu_kb(False))
        assert "🏆 Рейтинг" not in p_texts
    finally:
        vk._ctx_vertical.reset(token)
    # sport (по умолчанию) — исторические подписи
    texts = labels(vk._menu_kb(True))
    assert "➕ Создать тренировку" in texts
    assert "🏸 Тренировки" in texts
    assert "🏆 Рейтинг" in labels(vk._menu_kb(False))


async def test_vk_resolve_tenant_sets_vertical_ctx():
    import app.bots.vk as vk
    from app.db.engine import SessionLocal, engine
    with TestClient(app) as c:
        c.post("/api/tenants", json={
            "name": "VK Салон", "vertical": "beauty", "vk_group_id": 555001,
        }, headers=H)
    await engine.dispose()
    async with SessionLocal() as s:
        t = await vk._resolve_tenant(s, 555001)
        assert t is not None
        assert vk._ctx_vertical.get() == "beauty"


# ---------- UI мастеров в панели оператора ----------

def _op_login(c):
    login = c.post("/admin/platform/login", data={"token": "tok"},
                   follow_redirects=False)
    c.cookies.set("platform_token", login.cookies["platform_token"])
    return re.search(r'name="csrf" value="([^"]+)"',
                     c.get("/admin/platform").text).group(1)


def test_platform_masters_page_requires_auth():
    with TestClient(app) as c:
        r = c.get("/admin/platform/1/masters", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/admin/platform/login"


def test_platform_masters_add_toggle_flow():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон UI", "vertical": "beauty"}, headers=H).json()["id"]
        csrf = _op_login(c)
        page = c.get(f"/admin/platform/{tid}/masters")
        assert page.status_code == 200
        assert "Мастеров пока нет" in page.text

        # добавление
        r = c.post(f"/admin/platform/{tid}/masters/add", data={
            "csrf": csrf, "name": "Вера Кудрина",
            "specialty": "Колорист"}, follow_redirects=False)
        assert r.status_code == 303
        page = c.get(f"/admin/platform/{tid}/masters").text
        assert "Вера Кудрина" in page and "Колорист" in page
        assert "активен" in page

        # мастер доступен через API и на публичной странице после привязки
        masters = c.get(f"/api/tenants/{tid}/masters", headers=H).json()
        assert masters[0]["name"] == "Вера Кудрина"
        _mk_training(c, tid, title="Окрашивание", maxp=1,
                     master_id=masters[0]["id"])
        assert "Вера Кудрина" in c.get(f"/club/{tid}").text

        # скрыть / вернуть
        mid = masters[0]["id"]
        r = c.post(f"/admin/platform/{tid}/masters/{mid}/toggle",
                   data={"csrf": csrf}, follow_redirects=False)
        assert r.status_code == 303
        assert "скрыт" in c.get(f"/admin/platform/{tid}/masters").text
        c.post(f"/admin/platform/{tid}/masters/{mid}/toggle",
               data={"csrf": csrf})
        assert "активен" in c.get(f"/admin/platform/{tid}/masters").text


def test_platform_masters_add_rejects_bad_photo_and_csrf():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Салон Валидации"},
                     headers=H).json()["id"]
        csrf = _op_login(c)
        # javascript: в фото
        r = c.post(f"/admin/platform/{tid}/masters/add", data={
            "csrf": csrf, "name": "Злоумышленник",
            "photo_url": "javascript:alert(1)"})
        assert r.status_code == 400
        assert "http(s)-ссылка" in r.text
        # без csrf
        assert c.post(f"/admin/platform/{tid}/masters/add", data={
            "name": "Без токена"}).status_code == 403


class _FakeCbMsg:
    def __init__(self, chat_id):
        self.chat = type("C", (), {"id": chat_id, "type": "private"})()
        self.edits = []
        self.sent = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


class _FakeCb:
    def __init__(self, data, user_id, chat_id):
        self.data = data
        self.from_user = _FakeUser(user_id)
        self.message = _FakeCbMsg(chat_id)
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


async def test_tg_set_master_on_slot():
    import app.bots.telegram as tg
    from app.db.engine import SessionLocal, engine
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Setm", "vertical": "beauty",
            "admin_tg_id": 7601, "tg_chat_id": 87601}, headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Ольга"}).json()
        tr = _mk_training(c, tid, title="Слот", maxp=1)
    await engine.dispose()

    # не-админ получает отказ
    q_bad = _FakeCb(f"setm:{tr}:{m['id']}", user_id=111, chat_id=87601)
    await tg.cb_set_master(q_bad)
    assert q_bad.answers and "администратора" in q_bad.answers[0][0]

    # админ привязывает мастера
    q = _FakeCb(f"setm:{tr}:{m['id']}", user_id=7601, chat_id=87601)
    await tg.cb_set_master(q)
    assert any("Мастер Ольга" in t for t in q.message.edits)
    async with SessionLocal() as s:
        from app.repositories.repo import TenantRepository
        training = await TenantRepository(s, tid).get_training(tr)
        assert training.master_id == m["id"]

    # снятие мастера
    q2 = _FakeCb(f"setm:{tr}:0", user_id=7601, chat_id=87601)
    await tg.cb_set_master(q2)
    async with SessionLocal() as s:
        from app.repositories.repo import TenantRepository
        training = await TenantRepository(s, tid).get_training(tr)
        assert training.master_id is None


async def test_tg_offer_master_pick_only_with_masters():
    import app.bots.telegram as tg
    from app.db.engine import engine
    with TestClient(app) as c:
        tid_no = c.post("/api/tenants", json={"name": "Без мастеров"},
                        headers=H).json()["id"]
        tid_yes = c.post("/api/tenants", json={"name": "С мастерами"},
                         headers=H).json()["id"]
        c.post(f"/api/tenants/{tid_yes}/masters", headers=H,
               json={"name": "Тренер Иван"})
    await engine.dispose()

    msg = _FakeCbMsg(chat_id=1)
    await tg._offer_master_pick(msg, tid_no, 1)
    assert not msg.sent                      # мастеров нет — вопрос не задаём

    msg2 = _FakeCbMsg(chat_id=1)
    await tg._offer_master_pick(msg2, tid_yes, 1)
    assert msg2.sent and "Кто ведёт" in msg2.sent[0][0]
    kb = msg2.sent[0][1]["reply_markup"]
    btns = [b.text for row in kb.inline_keyboard for b in row]
    assert "Тренер Иван" in btns and "Без мастера" in btns


def test_builder_bundle_carries_vertical():
    with TestClient(app) as c:
        login = c.post("/admin/platform/login", data={"token": "tok"},
                       follow_redirects=False)
        c.cookies.set("platform_token", login.cookies["platform_token"])
        page = c.get("/admin/platform/builder")
        assert 'name="vertical"' in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = c.post("/admin/platform/builder", data={
            "csrf": csrf, "club_name": "Salon Client", "edition": "lite",
            "timezone": "Europe/Moscow", "tg_token": "1:X",
            "admin_tg_id": "42", "vertical": "beauty",
            "reminder_minutes": "60", "cancel_lock_minutes": "0",
            "brand_color": "#3a7bd5"})
        assert r.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(r.content))
        seed = z.read("seed.db")
        import os
        import sqlite3
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(seed)
            conn = sqlite3.connect(tmp)
            row = conn.execute("SELECT vertical FROM tenants").fetchone()
            conn.close()
            assert row == ("beauty",)
        finally:
            os.remove(tmp)
