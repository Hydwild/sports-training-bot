"""
Настройка ОДНОГО клиента не должна укладывать всю площадку.

Боевой случай: форма создания клуба ставит режим доставки webhook по
умолчанию. Стоило завести демо-клуб — и старт начал падать с RuntimeError,
потому что WEBHOOK_MASTER_SECRET не задан. Приложение не поднималось
вообще: ни одного клуба, ни страниц записи, ни панели. Railway при этом
показывал только «deployment failed» после таймаута healthcheck — без
намёка на причину.

Признак, по которому это опознаётся: один и тот же коммит сначала
разворачивается, а следующий деплой того же кода падает. Значит дело не в
коде, а в изменившемся состоянии базы.
"""
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import settings
from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clean():
    from app.api import rate_limit
    rate_limit._memory.clear()
    main_module._WEBHOOK_CONFIG_PROBLEMS.clear()
    yield
    rate_limit._memory.clear()
    main_module._WEBHOOK_CONFIG_PROBLEMS.clear()


async def _set_mode(tenant_id: int, mode: str) -> None:
    from app.db.engine import SessionLocal, engine
    from app.models.entities import Tenant
    await engine.dispose()
    async with SessionLocal() as s:
        t = await s.get(Tenant, tenant_id)
        t.tg_delivery_mode = mode
        await s.commit()


def _webhook_club(c, name: str) -> int:
    """Клуб в режиме webhook — ровно то, что делает форма по умолчанию."""
    import asyncio

    tid = c.post("/api/tenants", json={"name": name}, headers=H).json()["id"]
    asyncio.run(_set_mode(tid, "webhook"))
    return tid


def _reset(tenant_id: int) -> None:
    import asyncio
    asyncio.run(_set_mode(tenant_id, "polling"))


async def _webhook_club_async(c, name: str) -> int:
    tid = c.post("/api/tenants", json={"name": name}, headers=H).json()["id"]
    await _set_mode(tid, "webhook")
    return tid


async def test_missing_master_secret_is_reported_not_raised(monkeypatch):
    """Главное: список проблем вместо исключения."""
    monkeypatch.setattr(settings, "webhook_master_secret", "")
    with TestClient(app) as c:
        tid = await _webhook_club_async(c, "Клуб вебхук")
    try:
        problems = await main_module._check_client_webhook_config()
        assert problems, "проблема не замечена"
        assert "WEBHOOK_MASTER_SECRET" in problems[0]
    finally:
        await _set_mode(tid, "polling")


async def test_non_https_base_url_is_reported(monkeypatch):
    monkeypatch.setattr(settings, "webhook_master_secret", "x" * 40)
    monkeypatch.setattr(settings, "public_base_url", "http://testserver")
    with TestClient(app) as c:
        tid = await _webhook_club_async(c, "Клуб вебхук http")
    try:
        problems = await main_module._check_client_webhook_config()
        assert any("PUBLIC_BASE_URL" in p for p in problems), problems
    finally:
        await _set_mode(tid, "polling")


async def test_no_problems_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_master_secret", "x" * 40)
    monkeypatch.setattr(settings, "public_base_url", "https://bots.example")
    with TestClient(app) as c:
        tid = await _webhook_club_async(c, "Клуб вебхук ок")
    try:
        assert await main_module._check_client_webhook_config() == []
    finally:
        await _set_mode(tid, "polling")


async def test_no_problems_without_webhook_clubs(monkeypatch):
    """Без клубов на webhook общие секреты не нужны вовсе."""
    monkeypatch.setattr(settings, "webhook_master_secret", "")
    assert await main_module._check_client_webhook_config() == []


def test_app_starts_despite_broken_client_webhook(monkeypatch):
    """Ключевой инвариант: площадка поднимается и обслуживает остальных."""
    monkeypatch.setattr(settings, "webhook_master_secret", "")
    with TestClient(app) as c:
        tid = _webhook_club(c, "Клуб ломает старт")
    try:
        # новый запуск с уже испорченной конфигурацией в базе
        with TestClient(app) as c:
            r = c.get("/health")
            assert r.status_code in (200, 503)
            assert c.get("/promo").status_code == 200, "лендинг недоступен"
    finally:
        _reset(tid)


def test_health_reports_the_problem_without_failing_deploy(monkeypatch):
    """503 здесь означал бы новый провал деплоя — поэтому только флаг."""
    monkeypatch.setattr(settings, "webhook_master_secret", "")
    with TestClient(app) as c:
        tid = _webhook_club(c, "Клуб флаг")
    try:
        with TestClient(app) as c:
            body = c.get("/health").json()
            assert body["client_webhooks_ok"] is False
            assert body["status"] == "ok", "деплой не должен падать из-за клуба"
    finally:
        _reset(tid)


def test_health_flag_is_true_when_all_is_well():
    with TestClient(app) as c:
        assert c.get("/health").json()["client_webhooks_ok"] is True
