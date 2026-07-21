"""
Телефон перестал быть идентификатором записи и не хранится открытым текстом.

Раньше номер был самим user_id: он лежал в signups, subscribers и оценках
и уезжал в каждую суточную резервную копию, которая уходит в Telegram.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}
PHONE = "79141234567"


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


def _signup(c, phone=PHONE, name="Анна"):
    tid = c.post("/api/tenants", json={"name": "Клуб Приватности"},
                 headers=H).json()["id"]
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
    tr = c.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Занятие", "start_at": start, "max_participants": 5,
    }).json()["id"]
    r = c.post(f"/club/{tid}/signup", data={
        "consent": "1", "training_id": tr, "name": name, "phone": phone})
    assert r.status_code == 200, r.text
    return tid, tr


# ---------- шифрование ----------

def test_phone_roundtrip_and_index_is_stable():
    from app.core import phones

    enc, ver = phones.encrypt("+7 914 123-45-67")
    assert PHONE not in enc                       # шифротекст, а не номер
    assert phones.decrypt(enc, ver) == PHONE
    # разные написания одного номера дают один индекс
    assert phones.phone_index("+7 914 123-45-67") == phones.phone_index(PHONE)
    assert phones.phone_index("79140000000") != phones.phone_index(PHONE)


def test_decrypt_with_wrong_key_returns_empty_not_crash():
    from app.core import phones

    enc, _ver = phones.encrypt(PHONE)
    # версия ключа не та — карточка участника всё равно должна открыться
    assert phones.decrypt(enc, "env") == ""
    assert phones.decrypt("не-шифротекст", "jwt") == ""


# ---------- запись ----------

def test_signup_does_not_store_phone_as_identity():
    import asyncio

    with TestClient(app) as c:
        tid, tr = _signup(c)

        async def dump():
            from sqlalchemy import select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import Signup, Subscriber, WebCustomer
            await engine.dispose()
            async with SessionLocal() as s:
                sig = (await s.execute(select(Signup).where(
                    Signup.tenant_id == tid))).scalars().all()
                subs = (await s.execute(select(Subscriber).where(
                    Subscriber.tenant_id == tid))).scalars().all()
                cust = (await s.execute(select(WebCustomer).where(
                    WebCustomer.tenant_id == tid))).scalars().all()
                return ([(x.platform, x.user_id) for x in sig],
                        [(x.user_id, x.alias or "", x.name) for x in subs],
                        [(x.id, x.phone_enc, x.phone_index) for x in cust])

        sig, subs, cust = asyncio.run(dump())

        assert len(cust) == 1
        cid, enc, idx = cust[0]
        # номер не лежит открытым текстом нигде
        assert PHONE not in enc
        assert PHONE not in idx
        assert all(str(uid) != PHONE for _p, uid in sig)
        assert all(PHONE not in alias and PHONE not in name
                   for _uid, alias, name in subs)
        # запись ссылается на суррогатный id клиента
        assert [uid for _p, uid in sig] == [cid]
        assert tr


def test_same_phone_reuses_customer():
    """Тот же номер в другом написании — тот же клиент, без дубля."""
    import asyncio

    with TestClient(app) as c:
        tid, tr = _signup(c, phone="79147778899", name="Борис")
        # повторная запись тем же номером, записанным иначе
        c.post(f"/club/{tid}/signup", data={
            "consent": "1", "training_id": tr, "name": "Борис",
            "phone": "+7 914 777-88-99"})

        async def customers():
            from sqlalchemy import select

            from app.db.engine import SessionLocal, engine
            from app.models.entities import WebCustomer
            await engine.dispose()
            async with SessionLocal() as s:
                return (await s.execute(select(WebCustomer).where(
                    WebCustomer.tenant_id == tid))).scalars().all()

        assert len(asyncio.run(customers())) == 1


def test_trainer_still_sees_phone_in_admin_card():
    """Тренеру номер нужен, чтобы позвонить: он расшифровывается на лету."""
    import asyncio

    with TestClient(app) as c:
        tid, tr = _signup(c, phone="79145556677", name="Вера")

        async def card():
            from app.bots.views import training_card
            from app.db.engine import SessionLocal, engine
            from app.services.booking import BookingService
            await engine.dispose()
            async with SessionLocal() as s:
                svc = BookingService(s, tid)
                t = await svc.repo.get_training(tr)
                return await training_card(svc, t, for_admin=True)

        text = asyncio.run(card())
        assert "79145556677" in text
        assert "Вера" in text


def test_public_page_never_shows_phone():
    with TestClient(app) as c:
        tid, _tr = _signup(c, phone="79149998877", name="Галина")
        page = c.get(f"/club/{tid}").text
        assert "79149998877" not in page
        assert "9998877" not in page


# ---------- миграция исторических данных ----------

def test_old_phone_ids_are_remapped_by_migration(tmp_path):
    """Регресс на саму миграцию: записи, где user_id был номером, должны
    получить суррогатный id, а номер — уехать в web_customers."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    db = str(tmp_path / "legacy.db").replace("\\", "/")
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
           "ADMIN_API_TOKEN": "tok", "JWT_SECRET": "test-secret",
           "TG_TOKEN": "123456:TESTTOKEN", "PYTHONIOENCODING": "utf-8"}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args],
                              cwd=root, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=300)

    # схема на ревизии ДО перевода телефонов
    up = alembic("upgrade", "f92c4a1d5e73")
    assert up.returncode == 0, up.stderr[-2000:]

    import sqlite3
    con = sqlite3.connect(tmp_path / "legacy.db")

    def insert(table, **values):
        """Вставка с автозаполнением остальных NOT NULL-колонок: схема
        живая, и тест не должен ломаться от каждой новой колонки."""
        cols = con.execute(f"PRAGMA table_info({table})").fetchall()
        for _cid, name, ctype, notnull, default, pk in cols:
            if name in values or pk or not notnull or default is not None:
                continue
            values[name] = 0 if ctype.upper() in ("INTEGER", "BIGINT",
                                                  "BOOLEAN") else ""
        placeholders = ", ".join("?" * len(values))
        con.execute(f"INSERT INTO {table} ({', '.join(values)}) "
                    f"VALUES ({placeholders})", list(values.values()))

    insert("tenants", id=1, name="Старый клуб", timezone="UTC", is_active=1)
    insert("trainings", id=1, tenant_id=1, title="Игра",
           start_at="2026-01-01 10:00:00", max_participants=5)
    insert("signups", tenant_id=1, training_id=1, platform="web",
           user_id=int(PHONE), name="Анна", status="active", position=1)
    insert("subscribers", tenant_id=1, platform="web", user_id=int(PHONE),
           name="Анна", alias=f"Анна 📱+{PHONE}")
    con.commit()
    con.close()

    done = alembic("upgrade", "head")
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]

    con = sqlite3.connect(tmp_path / "legacy.db")
    cust = con.execute("SELECT id, phone_enc, phone_index, name "
                       "FROM web_customers").fetchall()
    sig = con.execute("SELECT user_id FROM signups").fetchall()
    subs = con.execute("SELECT user_id, alias FROM subscribers").fetchall()
    con.close()

    assert len(cust) == 1, cust
    cid, enc, idx, name = cust[0]
    assert name == "Анна"
    assert PHONE not in enc and PHONE not in idx
    assert sig == [(cid,)], "запись не переведена на суррогатный id"
    assert subs[0][0] == cid
    assert not subs[0][1], "телефон остался в подписи участника"

    from app.core import phones
    assert phones.decrypt(enc, "jwt") == PHONE   # тренер номер не потерял
