"""
Fail-closed по РЕАЛЬНЫМ данным: если версия, присутствующая в БД, не
читается (нет ключа или секрет подменён), новый WebCustomer не создаётся,
а readiness сообщает о проблеме. Источник истины — БД, а не только
PHONE_LEGACY_VERSIONS.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import phones
from app.core.phones import KeyUnavailable
from app.models.entities import Base, Tenant, WebCustomer
from app.repositories.repo import GlobalRepository, TenantRepository

PHONE = "79210001122"
OLD_JWT = "старый-jwt-секрет-достаточной-длины"
NEW_JWT = "новый-jwt-секрет-совсем-другой-1"
V2 = "выделенный-ключ-v2"


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


@pytest.fixture
def legacy_env(monkeypatch):
    """Прод-дефолт: только jwt из JWT_SECRET."""
    monkeypatch.setattr(phones.settings, "phone_keys", "")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "")
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "")
    monkeypatch.setattr(phones.settings, "jwt_secret", OLD_JWT)


async def _tenant(maker) -> int:
    async with maker() as s:
        t = Tenant(name="Клуб")
        s.add(t)
        await s.commit()
        return t.id


async def _seed_jwt_customer(maker, tid: int):
    """Клиент, созданный старым JWT (key_ver=index_ver='jwt')."""
    async with maker() as s:
        repo = TenantRepository(s, tid)
        uid = await repo.web_customer_id(PHONE, "Аня")
        await s.commit()
        return uid


async def test_jwt_row_plus_rotated_jwt_fails_closed(maker, legacy_env,
                                                     monkeypatch):
    """Точный сценарий дефекта: строка jwt, JWT сменили, старый ключ НЕ
    сохранён. Раньше missing_read_versions пуст, дубль создавался.
    Теперь создание нового клиента падает fail-closed."""
    tid = await _tenant(maker)
    await _seed_jwt_customer(maker, tid)

    # оператор сменил JWT, задал v2 активной, объявил legacy=jwt, но забыл
    # сохранить старый JWT как телефонный ключ версии jwt
    monkeypatch.setattr(phones.settings, "jwt_secret", NEW_JWT)
    monkeypatch.setattr(phones.settings, "phone_keys", f"v2:{V2}")
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "v2")
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "jwt")

    # missing_read_versions пуст (jwt формально выводится из нового JWT), но
    # по данным старая строка не читается -> отказ
    assert phones.missing_read_versions() == []
    async with maker() as s:
        repo = TenantRepository(s, tid)
        with pytest.raises(KeyUnavailable):
            await repo.web_customer_id("79210009999", "Новый")
        # новый клиент НЕ заведён
        rows = (await s.execute(select(WebCustomer))).scalars().all()
        assert len(rows) == 1        # только исходный


async def test_saved_old_key_allows_migration_no_dup(maker, legacy_env,
                                                     monkeypatch):
    """Старый ключ ЯВНО сохранён под меткой jwt, активна v2: прежний клиент
    находится, новый номер шифруется v2, дубля нет."""
    tid = await _tenant(maker)
    uid = await _seed_jwt_customer(maker, tid)

    monkeypatch.setattr(phones.settings, "jwt_secret", NEW_JWT)
    monkeypatch.setattr(phones.settings, "phone_keys",
                        f"jwt:{OLD_JWT},v2:{V2}")
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "v2")
    monkeypatch.setattr(phones.settings, "phone_legacy_versions", "jwt")

    async with maker() as s:
        repo = TenantRepository(s, tid)
        # тот же телефон -> находим прежнего, без дубля
        again = await repo.web_customer_id(PHONE, "Аня")
        assert again == uid
        # новый телефон -> создаётся под v2
        new_uid = await repo.web_customer_id("79210007777", "Пётр")
        await s.commit()
        row = (await s.execute(select(WebCustomer).where(
            WebCustomer.id == new_uid))).scalar_one()
        assert row.key_ver == "v2" and row.index_ver == "v2"
        # дублей нет
        assert len((await s.execute(select(WebCustomer))).scalars().all()) == 2


async def test_unknown_db_version_fails_closed(maker, legacy_env, monkeypatch):
    """Версия, реально присутствующая в БД, но неизвестная конфигурации —
    fail-closed, даже если её нет в PHONE_LEGACY_VERSIONS."""
    tid = await _tenant(maker)
    # вручную кладём строку версии v9, ключа которой нет
    async with maker() as s:
        enc, _ = phones.encrypt(PHONE)   # шифруем текущим (jwt), но пометим v9
        s.add(WebCustomer(tenant_id=tid, phone_index="deadbeef",
                          phone_enc=enc, key_ver="v9", index_ver="v9",
                          name="Икс"))
        await s.commit()

    # v9 нет в конфиге и нет в PHONE_LEGACY_VERSIONS — но она в данных
    assert phones.missing_read_versions() == []
    async with maker() as s:
        repo = TenantRepository(s, tid)
        with pytest.raises(KeyUnavailable):
            await repo.web_customer_id("79210004444", "Новый")


async def test_readiness_reports_bad_versions(maker, legacy_env, monkeypatch):
    tid = await _tenant(maker)
    await _seed_jwt_customer(maker, tid)
    monkeypatch.setattr(phones.settings, "jwt_secret", NEW_JWT)   # ключ подменён

    async with maker() as s:
        bad = await GlobalRepository(s).verify_web_keys()
    assert "jwt" in bad


async def test_readiness_clean_when_keys_ok(maker, legacy_env):
    tid = await _tenant(maker)
    await _seed_jwt_customer(maker, tid)
    async with maker() as s:
        assert await GlobalRepository(s).verify_web_keys() == []
