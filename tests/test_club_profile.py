"""
Витрина клуба на публичной странице записи (/club/{id}): обложка, описание,
адрес/телефон, лента мастеров; редактирование через панель оператора.
"""
import re

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


def _op_login(c):
    login = c.post("/admin/platform/login", data={"token": "tok"},
                   follow_redirects=False)
    c.cookies.set("platform_token", login.cookies["platform_token"])


def _edit_form(c, tid, **fields):
    page = c.get(f"/admin/platform/{tid}/edit")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    data = {"csrf": csrf, "club_name": fields.pop("club_name", "Клуб Витрины"),
            "timezone": "Europe/Moscow"}
    data.update(fields)
    return c.post(f"/admin/platform/{tid}/edit", data=data)


def test_profile_fields_saved_and_shown_on_club_page():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Клуб Витрины", "vertical": "beauty"},
            headers=H).json()["id"]
        _op_login(c)
        r = _edit_form(c, tid,
                       cover_url="https://example.com/cover.jpg",
                       about="Барбершоп в центре: стрижки и бритьё.",
                       address="ул. Ленина, 10",
                       contact_phone="+7 900 000-00-00")
        assert r.status_code == 200, r.text

        page = c.get(f"/club/{tid}").text
        assert 'src="https://example.com/cover.jpg"' in page
        assert "Барбершоп в центре" in page
        assert "ул. Ленина, 10" in page
        assert "+7 900 000-00-00" in page
        assert 'href="tel:+79000000000"' in page


def test_profile_absent_when_fields_empty():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Пустой Профиль"},
                     headers=H).json()["id"]
        page = c.get(f"/club/{tid}").text
        assert 'class="cover"' not in page
        assert 'class="about"' not in page
        assert 'class="biz-info"' not in page


def test_cover_url_must_be_http():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб XSS"},
                     headers=H).json()["id"]
        _op_login(c)
        r = _edit_form(c, tid, club_name="Клуб XSS",
                       cover_url="javascript:alert(1)")
        assert r.status_code == 400
        assert "http(s)-ссылкой" in r.text
        assert "javascript:alert" not in c.get(f"/club/{tid}").text


def _mk_training(c, tid, title="Слот", days=2, maxp=1, **extra):
    import datetime as dt
    start = (dt.datetime.now(dt.timezone.utc)
             + dt.timedelta(days=days)).isoformat()
    r = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": title, "start_at": start, "max_participants": maxp, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_funnel_screens_present_with_masters():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Воронки", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Ника"}).json()
        tr = _mk_training(c, tid, title="Стрижка", master_id=m["id"])
        page = c.get(f"/club/{tid}").text
        # три экрана воронки
        assert 'id="scr-home"' in page
        assert 'id="scr-masters"' in page
        assert 'id="scr-slots"' in page
        # меню в стиле YClients
        assert "Выбрать мастера" in page
        assert "Выбрать дату и время" in page
        # чип ближайшего свободного окна ведёт на слот
        assert f'data-slot="{tr}"' in page
        assert f'data-m="{m["id"]}"' in page
        # карточка слота с атрибутом мастера и якорем
        assert f'data-master="{m["id"]}" id="slot-{tr}"' in page
        assert 'id="mfilter"' in page


def test_funnel_absent_without_masters():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Без Воронки"},
                     headers=H).json()["id"]
        _mk_training(c, tid, title="Игра", maxp=5)
        page = c.get(f"/club/{tid}").text
        assert 'id="scr-home"' not in page      # прежний простой вид
        assert "Игра" in page


def test_funnel_full_slot_has_no_chip():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Занято", "vertical": "beauty"},
            headers=H).json()["id"]
        m = c.post(f"/api/tenants/{tid}/masters", headers=H,
                   json={"name": "Мастер Зоя"}).json()
        tr = _mk_training(c, tid, title="Занятый", master_id=m["id"])
        c.post(f"/club/{tid}/signup", data={
            "training_id": tr, "name": "Клиент", "phone": "79995556677"})
        page = c.get(f"/club/{tid}").text
        assert f'data-slot="{tr}"' not in page   # занятое окно не предлагаем
        assert "Свободных окон пока нет" in page


def test_masters_strip_shows_active_only():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={
            "name": "Салон Ленты", "vertical": "beauty"},
            headers=H).json()["id"]
        m1 = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Активная Анна", "specialty": "Барбер"}).json()
        m2 = c.post(f"/api/tenants/{tid}/masters", headers=H, json={
            "name": "Скрытая Мария"}).json()
        c.delete(f"/api/tenants/{tid}/masters/{m2['id']}", headers=H)

        page = c.get(f"/club/{tid}").text
        assert 'class="ms-strip"' in page
        assert "Активная Анна" in page and "Барбер" in page
        assert "Скрытая Мария" not in page
        assert m1["id"] > 0
