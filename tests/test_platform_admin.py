"""
Панель оператора (/admin/platform): вход по ADMIN_API_TOKEN, создание
клиентов без ручных curl к /api, продление оплаты. Отдельная от
тенант-админки (own cookie, own CSRF), см. app/admin/platform.py.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

TOKEN = "tok"  # см. tests/conftest.py: ADMIN_API_TOKEN=tok


@pytest.fixture(autouse=True)
def _clear_login_rate_limit():
    """Rate-limit на /admin/platform/login общий на весь процесс (по IP);
    в этом файле много тестов логинятся с одного и того же тестового IP —
    без сброса они бы упирались в лимит друг друга."""
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    yield
    api_routes._ip_hits.clear()


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def test_login_wrong_token_rejected_and_no_cookie():
    with TestClient(app) as c:
        r = c.post("/admin/platform/login", data={"token": "wrong"},
                   follow_redirects=False)
        assert r.status_code == 401
        assert "platform_token" not in r.cookies


def test_dashboard_requires_auth():
    with TestClient(app) as c:
        assert c.get("/admin/platform").status_code == 401
        assert c.get("/admin/platform/new").status_code == 401


def test_login_then_dashboard_and_create_client():
    with TestClient(app) as c:
        login = c.post("/admin/platform/login", data={"token": TOKEN},
                       follow_redirects=False)
        assert login.status_code == 302
        assert "platform_token" in login.cookies
        c.cookies.set("platform_token", login.cookies["platform_token"])

        dash = c.get("/admin/platform")
        assert dash.status_code == 200
        assert "Клиентов пока нет" in dash.text or "<table>" in dash.text

        form_page = c.get("/admin/platform/new")
        assert form_page.status_code == 200
        csrf = _csrf(form_page.text)

        r = c.post("/admin/platform/new", data={
            "csrf": csrf, "club_name": "Клуб Тест", "timezone": "Europe/Moscow",
            "tg_token": "123456:ABCDEF", "vk_token": "", "admin_tg_id": "555",
        })
        assert r.status_code == 200
        assert "Клуб «Клуб Тест» создан" in r.text
        assert "/club/" in r.text  # ссылка на публичную страницу

        dash2 = c.get("/admin/platform")
        assert "Клуб Тест" in dash2.text
        assert 'class="badge tg"' in dash2.text  # токен подхватился


def test_create_client_without_csrf_rejected():
    with TestClient(app) as c:
        login = c.post("/admin/platform/login", data={"token": TOKEN},
                       follow_redirects=False)
        c.cookies.set("platform_token", login.cookies["platform_token"])
        r = c.post("/admin/platform/new",
                   data={"club_name": "Без CSRF", "timezone": "Europe/Moscow"})
        assert r.status_code == 403


def test_create_client_bad_token_format_shows_error_but_keeps_tenant():
    with TestClient(app) as c:
        login = c.post("/admin/platform/login", data={"token": TOKEN},
                       follow_redirects=False)
        c.cookies.set("platform_token", login.cookies["platform_token"])
        csrf = _csrf(c.get("/admin/platform/new").text)
        r = c.post("/admin/platform/new", data={
            "csrf": csrf, "club_name": "Плохой токен",
            "timezone": "Europe/Moscow", "tg_token": "не-токен",
        })
        assert r.status_code == 400
        assert "создан" in r.text  # клуб не откатывается, только предупреждение
        # клуб реально появился в списке (хоть и без валидного токена)
        assert "Плохой токен" in c.get("/admin/platform").text


def test_billing_quick_update():
    with TestClient(app) as c:
        login = c.post("/admin/platform/login", data={"token": TOKEN},
                       follow_redirects=False)
        c.cookies.set("platform_token", login.cookies["platform_token"])
        csrf = _csrf(c.get("/admin/platform/new").text)
        c.post("/admin/platform/new", data={
            "csrf": csrf, "club_name": "Биллинг-клуб", "timezone": "Europe/Moscow"})

        dash = c.get("/admin/platform").text
        m = re.search(r'>Биллинг-клуб</td>.*?/admin/platform/(\d+)/billing',
                      dash, re.S)
        tid = m.group(1)
        csrf2 = _csrf(dash)

        r = c.post(f"/admin/platform/{tid}/billing",
                  data={"csrf": csrf2, "paid_until": "2030-01-01"},
                  follow_redirects=False)
        assert r.status_code == 302
        dash2 = c.get("/admin/platform").text
        assert "до 2030-01-01" in dash2


def _login_and_create(c, club_name="Клуб Изм", **extra):
    login = c.post("/admin/platform/login", data={"token": TOKEN},
                   follow_redirects=False)
    c.cookies.set("platform_token", login.cookies["platform_token"])
    csrf = _csrf(c.get("/admin/platform/new").text)
    data = {"csrf": csrf, "club_name": club_name, "timezone": "Europe/Moscow"}
    data.update(extra)
    r = c.post("/admin/platform/new", data=data)
    m = re.search(r"id=(\d+)", r.text)
    return int(m.group(1))


def test_edit_form_prefilled_with_current_values():
    with TestClient(app) as c:
        tid = _login_and_create(c, club_name="Клуб Заполнен",
                                tg_token="123456:ABCDEF", admin_tg_id="777")
        form = c.get(f"/admin/platform/{tid}/edit")
        assert form.status_code == 200
        assert 'value="Клуб Заполнен"' in form.text
        assert 'value="123456:ABCDEF"' in form.text
        assert 'value="777"' in form.text


def test_edit_updates_name_and_reflects_on_dashboard():
    with TestClient(app) as c:
        tid = _login_and_create(c, club_name="Старое имя")
        csrf = _csrf(c.get(f"/admin/platform/{tid}/edit").text)
        r = c.post(f"/admin/platform/{tid}/edit", data={
            "csrf": csrf, "club_name": "Новое имя", "timezone": "Europe/Moscow",
        })
        assert r.status_code == 200
        assert "Сохранено" in r.text
        dash = c.get("/admin/platform").text
        assert "Новое имя" in dash
        assert "Старое имя" not in dash


def test_edit_can_clear_token():
    with TestClient(app) as c:
        tid = _login_and_create(c, club_name="Клуб Отвязка",
                                tg_token="123456:ABCDEF")
        assert 'class="badge tg"' in c.get("/admin/platform").text

        csrf = _csrf(c.get(f"/admin/platform/{tid}/edit").text)
        r = c.post(f"/admin/platform/{tid}/edit", data={
            "csrf": csrf, "club_name": "Клуб Отвязка", "timezone": "Europe/Moscow",
            "tg_token": "", "vk_token": "",
        })
        assert r.status_code == 200
        assert re.search(r'name="tg_token" value=""', r.text)  # поле опустело

        dash = c.get("/admin/platform").text
        # у этого конкретного клуба бейджа TG больше нет (глобально другие
        # тесты могли создать свои клубы с TG — поэтому ищем в его строке)
        row = re.search(r"Клуб Отвязка.*?</tr>", dash, re.S).group(0)
        assert 'badge tg' not in row


def test_edit_updates_admin_ids():
    with TestClient(app) as c:
        tid = _login_and_create(c, club_name="Клуб Тренер")
        csrf = _csrf(c.get(f"/admin/platform/{tid}/edit").text)
        c.post(f"/admin/platform/{tid}/edit", data={
            "csrf": csrf, "club_name": "Клуб Тренер", "timezone": "Europe/Moscow",
            "admin_tg_id": "111", "admin_vk_id": "222",
        })
        form = c.get(f"/admin/platform/{tid}/edit").text
        assert 'value="111"' in form
        assert 'value="222"' in form
        assert "111" in c.get("/admin/platform").text


def test_edit_without_csrf_rejected():
    with TestClient(app) as c:
        tid = _login_and_create(c, club_name="Клуб CSRF")
        r = c.post(f"/admin/platform/{tid}/edit",
                   data={"club_name": "Клуб CSRF", "timezone": "Europe/Moscow"})
        assert r.status_code == 403


def test_edit_nonexistent_tenant_404():
    with TestClient(app) as c:
        login = c.post("/admin/platform/login", data={"token": TOKEN},
                       follow_redirects=False)
        c.cookies.set("platform_token", login.cookies["platform_token"])
        assert c.get("/admin/platform/999999/edit").status_code == 404


def test_edit_bad_token_keeps_name_change():
    with TestClient(app) as c:
        tid = _login_and_create(c, club_name="Клуб Частично")
        csrf = _csrf(c.get(f"/admin/platform/{tid}/edit").text)
        r = c.post(f"/admin/platform/{tid}/edit", data={
            "csrf": csrf, "club_name": "Имя Сохранилось", "timezone": "Europe/Moscow",
            "tg_token": "не-токен",
        })
        assert r.status_code == 400
        assert "Токен не принят" in r.text
        # имя всё равно поменялось, несмотря на ошибку токена
        assert "Имя Сохранилось" in c.get("/admin/platform").text


def test_rate_limit_on_login_attempts():
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    with TestClient(app) as c:
        codes = [c.post("/admin/platform/login", data={"token": "wrong"},
                        follow_redirects=False).status_code
                for _ in range(7)]
        assert codes.count(429) >= 1, codes
    api_routes._ip_hits.clear()
