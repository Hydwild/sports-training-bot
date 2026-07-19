"""Платежи: идемпотентность вебхука, авто-проставление paid, проверка IP."""
import datetime as dt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.repositories.repo import GlobalRepository, TenantRepository
from app.services.booking import BookingService
from app.services import payment_service
from app.services.payment_service import PaymentService
from app.services.payments import YooKassaProvider
from app.api.schemas import PaymentStart

H = {"x-admin-token": "tok"}


async def _setup(session):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб")
    await session.commit()
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    svc = BookingService(session, t.id)
    tr = await svc.create_training(title="T", start_at=now, location="",
                                   max_participants=5, platform="tg", user_id=1)
    tr.price_minor = 50000  # 500.00 RUB
    await session.commit()
    await svc.sign_up(tr.id, "tg", 100, "Аня")
    return t.id, tr.id


async def test_webhook_marks_paid_idempotent(session, monkeypatch):
    tenant_id, training_id = await _setup(session)
    repo = TenantRepository(session, tenant_id)
    # вручную создаём pending-платёж (как после start_payment)
    await repo.add_payment(
        training_id=training_id,
        signup_id=(await repo.get_user_signup(training_id, "tg", 100)).id,
        platform="tg", user_id=100, provider="yookassa",
        provider_payment_id="pay_1", amount_minor=50000, currency="RUB",
        status="pending")
    await session.commit()

    # обходим проверку IP (тестируем бизнес-логику зачисления) и подтверждаем
    # статус через "API" — тело вебхука само по себе больше не источник истины
    monkeypatch.setattr(YooKassaProvider, "verify_webhook",
                        lambda self, **kw: True)

    async def fake_fetch(self, pid):
        return {"status": "succeeded", "amount_minor": 50000, "currency": "RUB"}

    monkeypatch.setattr(YooKassaProvider, "fetch_payment_status", fake_fetch)
    payload = {"object": {"id": "pay_1", "status": "succeeded", "metadata": {}}}

    ok = await payment_service.handle_webhook(
        session, "yookassa", body=b"{}", headers={}, remote_ip="1.2.3.4",
        payload=payload)
    assert ok is True
    s = await repo.get_user_signup(training_id, "tg", 100)
    assert s.paid is True  # автоматически отмечен оплатившим

    # повторный вебхук (идемпотентность) — не падает, остаётся succeeded
    ok2 = await payment_service.handle_webhook(
        session, "yookassa", body=b"{}", headers={}, remote_ip="1.2.3.4",
        payload=payload)
    assert ok2 is True
    pay = await repo.get_payment_by_provider_id("pay_1")
    assert pay.status == "succeeded"


# ---------- Регресс: не доверяем телу вебхука напрямую ----------

async def test_webhook_not_confirmed_by_api_does_not_mark_paid(session, monkeypatch):
    """Тело вебхука говорит 'succeeded', но переподтвердить через API не
    удалось (сеть/провайдер недоступен) — платёж НЕ зачисляется."""
    tenant_id, training_id = await _setup(session)
    repo = TenantRepository(session, tenant_id)
    await repo.add_payment(
        training_id=training_id,
        signup_id=(await repo.get_user_signup(training_id, "tg", 100)).id,
        platform="tg", user_id=100, provider="yookassa",
        provider_payment_id="pay_2", amount_minor=50000, currency="RUB",
        status="pending")
    await session.commit()

    monkeypatch.setattr(YooKassaProvider, "verify_webhook",
                        lambda self, **kw: True)

    async def fake_fetch_fails(self, pid):
        return None  # провайдер недоступен / не удалось получить статус

    monkeypatch.setattr(YooKassaProvider, "fetch_payment_status", fake_fetch_fails)
    payload = {"object": {"id": "pay_2", "status": "succeeded", "metadata": {}}}

    ok = await payment_service.handle_webhook(
        session, "yookassa", body=b"{}", headers={}, remote_ip="1.2.3.4",
        payload=payload)
    assert ok is False
    s = await repo.get_user_signup(training_id, "tg", 100)
    assert s.paid is False
    pay = await repo.get_payment_by_provider_id("pay_2")
    assert pay.status == "pending"  # не тронут — ждём повтора вебхука


async def test_webhook_amount_mismatch_does_not_mark_paid(session, monkeypatch):
    """API подтверждает succeeded, но сумма не совпадает с тем, что мы сами
    сохранили при создании платежа — подозрительно, платёж НЕ зачисляется."""
    tenant_id, training_id = await _setup(session)
    repo = TenantRepository(session, tenant_id)
    await repo.add_payment(
        training_id=training_id,
        signup_id=(await repo.get_user_signup(training_id, "tg", 100)).id,
        platform="tg", user_id=100, provider="yookassa",
        provider_payment_id="pay_3", amount_minor=50000, currency="RUB",
        status="pending")
    await session.commit()

    monkeypatch.setattr(YooKassaProvider, "verify_webhook",
                        lambda self, **kw: True)

    async def fake_fetch_wrong_amount(self, pid):
        # сумма от "провайдера" отличается от сохранённой (50000)
        return {"status": "succeeded", "amount_minor": 1, "currency": "RUB"}

    monkeypatch.setattr(YooKassaProvider, "fetch_payment_status",
                        fake_fetch_wrong_amount)
    payload = {"object": {"id": "pay_3", "status": "succeeded", "metadata": {}}}

    ok = await payment_service.handle_webhook(
        session, "yookassa", body=b"{}", headers={}, remote_ip="1.2.3.4",
        payload=payload)
    assert ok is False
    pay = await repo.get_payment_by_provider_id("pay_3")
    assert pay.status == "pending"


def test_yookassa_fetch_payment_status_not_configured_returns_none():
    import asyncio
    p = YooKassaProvider()
    assert asyncio.run(p.fetch_payment_status("pay_x")) is None


def test_yookassa_ip_verification():
    p = YooKassaProvider()
    # адрес из доверенной сети ЮKassa
    assert p.verify_webhook(body=b"", headers={}, remote_ip="185.71.76.5") is True
    # посторонний адрес
    assert p.verify_webhook(body=b"", headers={}, remote_ip="8.8.8.8") is False
    assert p.verify_webhook(body=b"", headers={}, remote_ip=None) is False


# ---------- Регресс: незащищённые эндпойнты (внешний аудит) ----------

def test_trainings_list_requires_admin_token():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб"}, headers=H).json()["id"]
        # без токена — раньше отдавал полный список, включая черновики
        assert c.get(f"/api/tenants/{tid}/trainings").status_code == 401
        assert c.get(f"/api/tenants/{tid}/trainings?include_drafts=true"
                     ).status_code == 401
        # с токеном — как и раньше работает
        assert c.get(f"/api/tenants/{tid}/trainings", headers=H).status_code == 200


def test_training_signups_requires_admin_token():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб"}, headers=H).json()["id"]
        tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
            "title": "T",
            "start_at": (dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(days=1)).isoformat(),
            "max_participants": 5,
        }).json()["id"]
        # без токена раньше отдавал имена, статус явки и оплаты
        assert c.get(f"/api/tenants/{tid}/trainings/{tr}/signups"
                     ).status_code == 401
        assert c.get(f"/api/tenants/{tid}/trainings/{tr}/signups",
                     headers=H).status_code == 200


def test_payments_start_requires_admin_token():
    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб"}, headers=H).json()["id"]
        # без токена раньше можно было инициировать реальный платёж
        # с произвольным return_url за чужого user_id
        r = c.post(f"/api/tenants/{tid}/payments/start",
                   json={"training_id": 1, "user_id": 1,
                        "return_url": "https://example.com"})
        assert r.status_code == 401


def test_payment_start_rejects_non_http_return_url():
    with pytest.raises(ValidationError):
        PaymentStart(training_id=1, user_id=1,
                    return_url="javascript:alert(1)")


async def test_start_payment_requires_existing_signup(session):
    tenant_id, training_id = await _setup_paid_training(session)
    psvc = PaymentService(session, tenant_id)
    # user_id=999 никогда не записывался на тренировку
    with pytest.raises(ValueError, match="Нет записи"):
        await psvc.start_payment(
            training_id=training_id, platform="tg", user_id=999,
            provider_name="yookassa", return_url="https://example.com")


async def _setup_paid_training(session):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб Оплаты")
    await session.commit()
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    svc = BookingService(session, t.id)
    tr = await svc.create_training(title="T", start_at=now, location="",
                                   max_participants=5, platform="tg", user_id=1)
    tr.price_minor = 50000
    await session.commit()
    return t.id, tr.id
