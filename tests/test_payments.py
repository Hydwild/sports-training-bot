"""Платежи: идемпотентность вебхука, авто-проставление paid, проверка IP."""
import datetime as dt
import pytest
from app.repositories.repo import GlobalRepository, TenantRepository
from app.services.booking import BookingService
from app.services import payment_service
from app.services.payments import YooKassaProvider


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

    # обходим проверку IP (тестируем бизнес-логику зачисления)
    monkeypatch.setattr(YooKassaProvider, "verify_webhook",
                        lambda self, **kw: True)
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


def test_yookassa_ip_verification():
    p = YooKassaProvider()
    # адрес из доверенной сети ЮKassa
    assert p.verify_webhook(body=b"", headers={}, remote_ip="185.71.76.5") is True
    # посторонний адрес
    assert p.verify_webhook(body=b"", headers={}, remote_ip="8.8.8.8") is False
    assert p.verify_webhook(body=b"", headers={}, remote_ip=None) is False
