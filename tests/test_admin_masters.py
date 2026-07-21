"""
Мастера и тренеры в админке владельца клуба.

Раньше это умел только оператор платформы (/admin/platform): владелец не
мог сам завести мастера через веб и просил об этом — при том, что данные
принадлежат ему.
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


def _club_owner(c, tg_id, vertical="beauty"):
    tid = c.post("/api/tenants", json={"name": "Салон Мастеров",
                                       "vertical": vertical},
                 headers=H).json()["id"]
    c.post(f"/api/tenants/{tid}/members", headers=H,
           json={"tg_user_id": tg_id, "role": "owner", "name": "Владелец"})
    c.post("/admin/auth/dev", data={"tg_user_id": tg_id})
    return tid


def _csrf(page_text):
    return re.search(r'name="csrf" value="([^"]+)"', page_text).group(1)


def test_owner_can_add_and_hide_master():
    with TestClient(app) as c:
        tid = _club_owner(c, 5001)

        page = c.get("/admin/masters")
        assert page.status_code == 200
        assert "Мастера салона" in page.text or "астер" in page.text

        r = c.post("/admin/masters/add", data={
            "csrf": _csrf(page.text), "name": "Ирина Ветрова",
            "specialty": "Колорист", "bio": "Опыт 7 лет"},
            follow_redirects=False)
        assert r.status_code == 303

        listing = c.get("/admin/masters").text
        assert "Ирина Ветрова" in listing and "Колорист" in listing
        # мастер сразу виден клиенту на публичной странице
        assert "Ирина Ветрова" in c.get(f"/club/{tid}").text

        mid = c.get(f"/api/tenants/{tid}/masters", headers=H).json()[0]["id"]
        c.post(f"/admin/masters/{mid}/toggle",
               data={"csrf": _csrf(listing)}, follow_redirects=False)
        assert "скрыт" in c.get("/admin/masters").text


def test_masters_page_uses_vertical_wording():
    with TestClient(app) as c:
        _club_owner(c, 5002, vertical="sport")
        assert "ренер" in c.get("/admin/masters").text


def test_photo_must_be_http_link():
    with TestClient(app) as c:
        _club_owner(c, 5003)
        page = c.get("/admin/masters")
        r = c.post("/admin/masters/add", data={
            "csrf": _csrf(page.text), "name": "Злой Мастер",
            "photo_url": "javascript:alert(1)"})
        assert r.status_code == 400
        assert "http(s)" in r.text
        assert "javascript:alert" not in c.get("/admin/masters").text


def test_other_club_owner_cannot_touch_foreign_masters():
    """Изоляция клубов: страница показывает только своих."""
    with TestClient(app) as c:
        _club_owner(c, 5004)
        page = c.get("/admin/masters")
        c.post("/admin/masters/add", data={
            "csrf": _csrf(page.text), "name": "Свой Мастер"})

        _club_owner(c, 5005)     # вход как владелец ДРУГОГО клуба
        assert "Свой Мастер" not in c.get("/admin/masters").text


def test_masters_require_auth():
    with TestClient(app) as c:
        c.cookies.clear()
        assert c.get("/admin/masters",
                     follow_redirects=False).status_code in (302, 303, 401, 403)
