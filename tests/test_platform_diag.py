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
    """В отчёте только имена типов, шаблоны маршрутов и числа.

    Проверяем СТРУКТУРНО, а не поиском подстроки: тестовый ADMIN_API_TOKEN
    равен "tok", и он входит в безобидное `{token}` из шаблона маршрута
    `/club/{tenant_id}/m/{token}`. Подстрочная проверка на таком коротком
    секрете даёт ложные срабатывания и ничего не доказывает. Настоящий
    инвариант другой: все ЗНАЧЕНИЯ в отчёте — числа и флаги, строк среди
    них нет вовсе, поэтому утечь значению просто некуда."""
    from app.core.config import settings

    with TestClient(app) as c:
        _login(c)
        body = c.get("/admin/platform/diag").json()
        text = c.get("/admin/platform/diag").text

    def _assert_no_string_values(value, path="") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                _assert_no_string_values(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _assert_no_string_values(v, f"{path}[{i}]")
        else:
            assert not isinstance(value, str), (
                f"{path}: строковое значение {value!r} — в отчёт могут "
                "утечь данные, оставляйте только числа и флаги")

    _assert_no_string_values(body)
    # длинный секрет проверяем и подстрокой — здесь совпадение не случайно
    assert settings.jwt_secret not in text


def test_matplotlib_not_loaded_at_startup():
    """Страж ленивого импорта: matplotlib весит ~35 МБ RSS и не должен
    подниматься, пока никто не запросил график."""
    with TestClient(app) as c:
        _login(c)
        assert c.get("/admin/platform/diag").json()["matplotlib_loaded"] is False
