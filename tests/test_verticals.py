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
