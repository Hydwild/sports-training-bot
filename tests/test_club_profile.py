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
