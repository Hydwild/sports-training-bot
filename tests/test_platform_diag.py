"""
Диагностика памяти в панели оператора: доступна только владельцу площадки
и не раскрывает ничего, кроме счётчиков.
"""
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


def _login(c):
    c.post("/admin/platform/login", data={"token": "tok"})


def test_diag_requires_platform_auth():
    with TestClient(app) as c:
        c.cookies.clear()
        r = c.get("/admin/platform/diag", follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403)


def test_diag_reports_memory_facts():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/admin/platform/diag")
        assert r.status_code == 200
        body = r.json()
        for key in ("rss_mb", "uptime_min", "modules", "threads",
                    "asyncio_tasks", "gc_tracked", "top_objects",
                    "matplotlib_loaded"):
            assert key in body, f"нет поля {key}"
        assert body["modules"] > 0
        assert isinstance(body["top_objects"], dict)
        # счётчики, а не содержимое: значения — числа
        assert all(isinstance(v, int) for v in body["top_objects"].values())


def test_diag_leaks_no_secrets():
    """В отчёте только имена типов и числа — ни токенов, ни телефонов."""
    from app.core.config import settings

    with TestClient(app) as c:
        _login(c)
        text = c.get("/admin/platform/diag").text
        assert settings.admin_api_token not in text
        assert settings.jwt_secret not in text


def test_matplotlib_not_loaded_at_startup():
    """Страж ленивого импорта: matplotlib весит ~35 МБ RSS и не должен
    подниматься, пока никто не запросил график."""
    with TestClient(app) as c:
        _login(c)
        assert c.get("/admin/platform/diag").json()["matplotlib_loaded"] is False
