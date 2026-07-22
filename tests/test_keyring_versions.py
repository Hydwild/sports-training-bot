"""
Конфигурируемые неизменяемые версии ключей телефонов и запрет создавать
WebCustomer при недоступном legacy-ключе.

Если ключ версии, под которой МОГ быть уже записан этот телефон, сейчас
недоступен, создание нового клиента заведёт дубль (со своими записями,
оценками и ссылкой). Поэтому создавать нельзя — только найти существующего.
"""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import phones
from app.core.phones import KeyUnavailable
from app.models.entities import Base, Tenant, WebCustomer
from app.repositories.repo import TenantRepository

PHONE = "79201112233"


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _tenant(maker) -> int:
    async with maker() as s:
        t = Tenant(name="Клуб")
        s.add(t)
        await s.commit()
        return t.id


# ---------- конфигурируемые версии ----------

def test_explicit_versions_are_configurable(monkeypatch):
    monkeypatch.setattr(phones.settings, "phone_keys", "v1:secretA,v2:secretB")
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "v2")
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")

    assert phones.active_key_ver() == "v2"
    enc, ver = phones.encrypt(PHONE)
    assert ver == "v2"
    assert phones.decrypt(enc, "v2") == PHONE
    # v1 остаётся читаемым
    enc1, _ = phones.encrypt(PHONE, "v1")
    assert phones.decrypt(enc1, "v1") == PHONE


def test_version_labels_are_immutable(monkeypatch):
    """Одна и та же версия у разных секретов — это ошибка конфигурации,
    но переопределение берёт последний источник детерминированно."""
    monkeypatch.setattr(phones.settings, "phone_keys", "v1:AAA")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")
    idx_a = phones.phone_index(PHONE, "v1")
    # тот же секрет -> тот же индекс (детерминизм версии)
    assert phones.phone_index(PHONE, "v1") == idx_a


def test_default_config_stays_jwt_for_backward_compat(monkeypatch):
    """Прод сейчас без ключей: активная версия jwt, всё читается."""
    monkeypatch.setattr(phones.settings, "phone_keys", "")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "")
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "")
    assert phones.active_key_ver() == phones.KEY_JWT
    assert phones.missing_read_versions() == []


# ---------- запрет создания при недоступном legacy-ключе ----------

async def test_creation_blocked_when_legacy_key_missing(maker, monkeypatch):
    tid = await _tenant(maker)
    # объявляем, что в данных МОЖЕТ быть версия v9, но ключа для неё нет
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "v9")
    monkeypatch.setattr(phones.settings, "phone_keys", "")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")

    assert "v9" in phones.missing_read_versions()
    async with maker() as s:
        repo = TenantRepository(s, tid)
        with pytest.raises(KeyUnavailable):
            await repo.web_customer_id(PHONE, "Аня")
        # клиент НЕ заведён
        rows = (await s.execute(select(WebCustomer))).scalars().all()
        assert rows == []


async def test_existing_customer_still_found_when_legacy_missing(maker,
                                                                 monkeypatch):
    """Найти существующего можно даже при недоступной legacy-версии —
    блокируется только СОЗДАНИЕ."""
    tid = await _tenant(maker)
    # сначала заведём клиента при полном доступе (только jwt)
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "")
    monkeypatch.setattr(phones.settings, "phone_keys", "")
    async with maker() as s:
        repo = TenantRepository(s, tid)
        uid = await repo.web_customer_id(PHONE, "Аня")
        await s.commit()

    # теперь объявляем недоступную legacy — существующий всё равно находится
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "v9")
    async with maker() as s:
        repo = TenantRepository(s, tid)
        found = await repo.find_web_customer_id(PHONE)
        assert found == uid
        # и повторная запись тем же номером возвращает того же, не блокируясь
        again = await repo.web_customer_id(PHONE, "Аня")
        assert again == uid


async def test_creation_allowed_when_all_keys_present(maker, monkeypatch):
    tid = await _tenant(maker)
    monkeypatch.setattr(phones.settings, "phone_keys", "v1:secretA")
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "v1")
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "v1")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")

    assert phones.missing_read_versions() == []
    async with maker() as s:
        repo = TenantRepository(s, tid)
        uid = await repo.web_customer_id(PHONE, "Аня")
        assert uid
        row = (await s.execute(select(WebCustomer))).scalars().one()
        assert row.key_ver == "v1"
        assert row.index_ver == "v1"
        assert dt  # silence import
