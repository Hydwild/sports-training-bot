"""
Конфигурация proxy headers проверяема: в явной proxy-среде без
TRUSTED_PROXIES старт падает, /health показывает безопасный boolean.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_proxy_env_without_trusted_proxies_fails_fast(monkeypatch):
    monkeypatch.setattr(settings, "admin_dev_login", False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "trusted_proxies", "")
    with pytest.raises(RuntimeError) as e:
        settings.assert_proxy_config()
    assert "TRUSTED_PROXIES" in str(e.value)


def test_proxy_env_with_trusted_proxies_ok(monkeypatch):
    monkeypatch.setattr(settings, "admin_dev_login", False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "trusted_proxies", "*")
    settings.assert_proxy_config()          # не бросает


def test_no_proxy_env_does_not_require_it(monkeypatch):
    monkeypatch.setattr(settings, "admin_dev_login", False)
    for m in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID",
              "RENDER", "RENDER_SERVICE_ID"):
        monkeypatch.delenv(m, raising=False)
    monkeypatch.setattr(settings, "trusted_proxies", "")
    settings.assert_proxy_config()          # локально/VPS без прокси — ок


def test_dev_mode_skips_proxy_check(monkeypatch):
    monkeypatch.setattr(settings, "admin_dev_login", True)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "trusted_proxies", "")
    settings.assert_proxy_config()          # dev не мешаем


def test_health_exposes_safe_booleans_only(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8, 172.16.0.1")
    with TestClient(app) as c:
        data = c.get("/health").json()
        assert data["proxy_headers_configured"] is True
        assert "keys_ok" in data
        # адреса/значения не раскрыты
        body = c.get("/health").text
        assert "10.0.0.0" not in body and "172.16" not in body


def test_health_proxy_false_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "")
    with TestClient(app) as c:
        assert c.get("/health").json()["proxy_headers_configured"] is False
