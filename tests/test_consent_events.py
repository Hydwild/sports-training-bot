"""
Журнал согласий и честность страницы обработки данных.

Галочку на форме проверяли, но факт согласия нигде не сохраняли: доказать,
что человек её ставил, было нечем. Плюс страница обещала «третьим лицам не
передаём», хотя Railway, Telegram и ВК фактически участвуют в обработке.
"""
import asyncio
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models.entities import ConsentEvent

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.api import routes as api_routes
    api_routes._ip_hits.clear()
    yield
    api_routes._ip_hits.clear()


def _events(purpose: str | None = None, tenant_id: int | None = None):
    async def load():
        from app.db.engine import SessionLocal, engine
        await engine.dispose()
        async with SessionLocal() as s:
            stmt = select(ConsentEvent)
            if purpose:
                stmt = stmt.where(ConsentEvent.purpose == purpose)
            if tenant_id is not None:
                stmt = stmt.where(ConsentEvent.tenant_id == tenant_id)
            return (await s.execute(stmt)).scalars().all()

    return asyncio.run(load())


def _club_with_slot(c, name="Клуб Согласий"):
    tid = c.post("/api/tenants", json={"name": name}, headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Занятие", "start_at": start, "max_participants": 5,
    }).json()["id"]
    return tid, tr


def test_booking_records_consent_with_version_and_text():
    with TestClient(app) as c:
        tid, tr = _club_with_slot(c)
        c.post(f"/club/{tid}/signup", data={
            "consent": "1", "training_id": tr, "name": "Игорь",
            "phone": "79180001111"})

        rows = _events("booking", tid)
        assert len(rows) == 1
        ev = rows[0]
        assert ev.tenant_id == tid
        assert ev.platform == "web"
        assert ev.user_id, "субъект не записан"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ev.policy_version)
        assert "имени и телефона для записи" in ev.consent_text
        assert ev.accepted_at is not None


def test_consent_stores_customer_id_not_phone():
    """Телефон уже лежит зашифрованным в web_customers — дублировать его
    в журнал значило бы вернуть открытый номер в базу."""
    phone = "79180002222"
    with TestClient(app) as c:
        tid, tr = _club_with_slot(c, name="Клуб Без Номера")
        c.post(f"/club/{tid}/signup", data={
            "consent": "1", "training_id": tr, "name": "Анна",
            "phone": phone})

        ev = _events("booking", tid)[0]
        assert str(ev.user_id) != phone
        assert phone not in (ev.consent_text or "")


def test_refused_form_records_nothing():
    with TestClient(app) as c:
        tid, tr = _club_with_slot(c, name="Клуб Отказа")
        r = c.post(f"/club/{tid}/signup", data={
            "training_id": tr, "name": "Без Галочки", "phone": "79180003333"})
        assert r.status_code == 400
        assert _events("booking", tid) == []


def test_consent_is_atomic_with_booking(monkeypatch):
    """Если согласие не сохранилось — записи тоже не должно остаться."""
    from app.repositories.repo import TenantRepository

    async def boom(self, **kwargs):
        raise RuntimeError("сбой записи согласия")

    with TestClient(app) as c:
        tid, tr = _club_with_slot(c, name="Клуб Атомарности")
        monkeypatch.setattr(TenantRepository, "record_consent", boom)
        with pytest.raises(RuntimeError):
            c.post(f"/club/{tid}/signup", data={
                "consent": "1", "training_id": tr, "name": "Пропавший",
                "phone": "79180004444"})
        monkeypatch.undo()

        signups = c.get(f"/api/tenants/{tid}/trainings/{tr}/signups",
                        headers=H).json()
        assert signups == [], "запись сохранилась без согласия"


def test_platform_review_records_consent_without_tenant():
    with TestClient(app) as c:
        before = len(_events("platform_review"))
        c.post("/reviews", data={"consent": "1", "name": "Согласная Зинаида",
                                 "rating": "5", "text": "Удобно"})
        rows = _events("platform_review")
        assert len(rows) == before + 1
        ev = rows[-1]
        assert ev.tenant_id is None      # форма вне клубов
        assert ev.source == "platform-form"


def test_privacy_page_names_actual_processors():
    with TestClient(app) as c:
        page = c.get("/privacy").text
        # больше не обещаем недоказуемого
        assert "не передаём данные третьим лицам" not in page
        for name in ("Railway", "Telegram", "ВКонтакте"):
            assert name in page
        # честно про удалённое в старых копиях
        assert "резервных копиях они остаются" in page
        # сроки хранения по видам данных
        assert "журнал согласий" in page
        assert "30 дней" in page
        # пометка про юриста
        assert "юрист" in page
