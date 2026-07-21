"""
Заголовки безопасности на всех ответах.

CSP закрывает основные векторы (object, base-uri, кликджекинг, форма
уходит только к нам), Permissions-Policy выключает камеру/микрофон/оплату,
Referrer-Policy не даёт адресам утекать на сторонние сайты. Инлайновые
стили и скрипты приложение использует осознанно — это отражено в политике.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clean():
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


def test_public_pages_carry_security_headers():
    with TestClient(app) as c:
        for path in ("/promo", "/faq", "/privacy", "/reviews"):
            r = c.get(path)
            csp = r.headers["content-security-policy"]
            assert "default-src 'self'" in csp
            assert "object-src 'none'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "form-action 'self'" in csp
            assert r.headers["permissions-policy"].startswith("accelerometer=()")
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert "referrer-policy" in r.headers


def test_csp_allows_telegram_login_widget():
    """Виджет входа в админку грузит скрипт с telegram.org и открывает
    iframe oauth.telegram.org — политика должна их пропускать."""
    from app.main import _CSP
    assert "https://telegram.org" in _CSP
    assert "frame-src https://oauth.telegram.org" in _CSP


def test_manage_page_stays_no_store_over_default():
    """Общий Referrer-Policy мягкий, но страница с личными данными сама
    ужесточает его до no-referrer (блок 1) — общий заголовок не должен это
    перебивать."""
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Заголовков"},
                     headers=H).json()["id"]
        start = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(days=2)).isoformat()
        tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "Занятие", "start_at": start, "max_participants": 5,
        }).json()["id"]
        import re
        r = c.post(f"/club/{tid}/signup", data={
            "consent": "1", "training_id": tr, "name": "Ольга",
            "phone": "79190001234"})
        link = re.search(r'href="(/club/\d+/m/[\w-]+)"', r.text).group(1)
        c.get(link)
        page = c.get(f"/club/{tid}/manage")
        assert page.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in page.headers["cache-control"]


def test_error_response_does_not_leak_secrets():
    """detail ошибки — константа, не сам токен/телефон, и стектрейс в
    ответ не уходит (FastAPI без debug)."""
    with TestClient(app, raise_server_exceptions=False) as c:
        # присылаем ЗАВЕДОМО НЕВЕРНЫЙ токен и убеждаемся, что он не эхонется
        # обратно в теле ошибки (иначе он попал бы в логи прокси/браузера)
        r = c.post("/api/tenants", json={"name": "x"},
                   headers={"x-admin-token": "SECRET-must-not-echo-9x7q"})
        assert r.status_code == 401
        assert "Traceback" not in r.text
        assert "SECRET-must-not-echo-9x7q" not in r.text
