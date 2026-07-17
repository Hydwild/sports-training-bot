"""
Панель оператора (/admin/platform): вход по ADMIN_API_TOKEN, создание
клиентов без ручных curl к /api, продление оплаты. Отдельная от
тенант-админки (own cookie, own CSRF), см. app/admin/platform.py.
"""
import re

from fastapi.testclient import TestClient

from app.main import app

TOKEN = "tok"  # см. tests/conftest.py: ADMIN_API_TOKEN=tok


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


def test_rate_limit_on_login_attempts():
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    with TestClient(app) as c:
        codes = [c.post("/admin/platform/login", data={"token": "wrong"},
                        follow_redirects=False).status_code
                for _ in range(7)]
        assert codes.count(429) >= 1, codes
    api_routes._ip_hits.clear()
