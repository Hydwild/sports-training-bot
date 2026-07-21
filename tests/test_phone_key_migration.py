"""
Команда перевода телефонов на новый ключ: dry-run / apply / verify.

Проверяем то, из-за чего такие миграции обычно и теряют данные:
повторный запуск, нечитаемые строки и клиентов-дублей с одним номером.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import phones
from app.models.entities import Base, WebCustomer
from scripts import migrate_phone_keys as mig

OLD_JWT = "старый-jwt-секрет-достаточной-длины"
NEW_KEY = "выделенный-ключ-телефонов-v1"


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
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "jwt_secret", OLD_JWT)


def _switch_to_v1(monkeypatch):
    """Переход: выделенный ключ добавлен, старый остаётся читаемым."""
    monkeypatch.setattr(phones.settings, "phone_enc_key", NEW_KEY)
    monkeypatch.setattr(phones.settings, "phone_keyring", f"jwt:{OLD_JWT}")


async def _seed(maker, phone: str, tenant_id: int = 1) -> int:
    """Клиент, записанный старым ключом (версия jwt)."""
    from app.models.entities import Tenant

    async with maker() as s:
        if not (await s.execute(select(Tenant).where(
                Tenant.id == tenant_id))).scalar_one_or_none():
            s.add(Tenant(id=tenant_id, name=f"Клуб {tenant_id}"))
            await s.flush()
        enc, ver = phones.encrypt(phone)
        row = WebCustomer(tenant_id=tenant_id, phone_index=phones.phone_index(phone),
                          phone_enc=enc, key_ver=ver, index_ver=ver, name="Клиент")
        s.add(row)
        await s.commit()
        return row.id


async def _run(maker, monkeypatch, mode: str) -> int:
    monkeypatch.setattr(mig, "SessionLocal", maker)
    return await mig.main_async(mode)


async def test_dry_run_changes_nothing(maker, legacy_env, monkeypatch):
    cid = await _seed(maker, "79170001111")
    _switch_to_v1(monkeypatch)

    assert await _run(maker, monkeypatch, "dry-run") == 0
    async with maker() as s:
        row = await s.get(WebCustomer, cid)
        assert row.key_ver == phones.KEY_JWT, "пробный прогон изменил базу"
        assert row.index_ver == phones.KEY_JWT


async def test_apply_then_verify_and_idempotent(maker, legacy_env, monkeypatch):
    phone = "79170002222"
    cid = await _seed(maker, phone)
    _switch_to_v1(monkeypatch)

    assert await _run(maker, monkeypatch, "verify") == 1     # ещё не мигрировано
    assert await _run(maker, monkeypatch, "apply") == 0

    async with maker() as s:
        row = await s.get(WebCustomer, cid)
        assert row.key_ver == phones.KEY_V1
        assert row.index_ver == phones.KEY_V1
        assert phones.decrypt(row.phone_enc, row.key_ver) == phone
        assert row.phone_index == phones.phone_index(phone, phones.KEY_V1)

    assert await _run(maker, monkeypatch, "verify") == 0
    # повторный запуск ничего не портит
    assert await _run(maker, monkeypatch, "apply") == 0
    assert await _run(maker, monkeypatch, "verify") == 0


async def test_after_migration_jwt_rotation_is_safe(maker, legacy_env,
                                                    monkeypatch):
    phone = "79170003333"
    cid = await _seed(maker, phone)
    _switch_to_v1(monkeypatch)
    await _run(maker, monkeypatch, "apply")

    # переход завершён — связку убрали, JWT ротировали
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "jwt_secret", "новый-секрет-jwt-длинный")

    async with maker() as s:
        row = await s.get(WebCustomer, cid)
        assert phones.decrypt(row.phone_enc, row.key_ver) == phone


async def test_unreadable_row_blocks_apply(maker, legacy_env, monkeypatch):
    """Нет ключа версии — применять нельзя: номер будет потерян."""
    await _seed(maker, "79170004444")
    # выделенный ключ добавлен, а старый НЕ положили в связку
    monkeypatch.setattr(phones.settings, "phone_enc_key", NEW_KEY)
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "jwt_secret", "другой-секрет-совсем")

    assert await _run(maker, monkeypatch, "apply") == 2
    async with maker() as s:
        row = (await s.execute(select(WebCustomer))).scalars().one()
        assert row.key_ver == phones.KEY_JWT, "строка изменена вопреки отчёту"


async def test_duplicate_customers_block_apply(maker, legacy_env, monkeypatch):
    """Два клиента с одним номером не сливаются молча: у них разные
    записи, оценки и ссылки управления."""
    phone = "79170005555"
    await _seed(maker, phone)
    # второй клиент того же клуба с тем же номером, но уже на новом ключе
    _switch_to_v1(monkeypatch)
    async with maker() as s:
        enc, ver = phones.encrypt(phone)
        s.add(WebCustomer(tenant_id=1, phone_index=phones.phone_index(phone),
                          phone_enc=enc, key_ver=ver, index_ver=ver,
                          name="Дубль"))
        await s.commit()

    assert await _run(maker, monkeypatch, "dry-run") == 2
    assert await _run(maker, monkeypatch, "apply") == 2
    async with maker() as s:
        rows = (await s.execute(select(WebCustomer))).scalars().all()
        assert {r.key_ver for r in rows} == {phones.KEY_JWT, phones.KEY_V1}


async def test_same_phone_in_different_clubs_is_not_a_duplicate(
        maker, legacy_env, monkeypatch):
    phone = "79170006666"
    await _seed(maker, phone, tenant_id=1)
    await _seed(maker, phone, tenant_id=2)
    _switch_to_v1(monkeypatch)

    assert await _run(maker, monkeypatch, "apply") == 0
    assert await _run(maker, monkeypatch, "verify") == 0


async def test_report_never_prints_phone_or_key(maker, legacy_env, monkeypatch,
                                                capsys):
    phone = "79170007777"
    await _seed(maker, phone)
    _switch_to_v1(monkeypatch)
    await _run(maker, monkeypatch, "dry-run")

    out = capsys.readouterr().out
    assert phone not in out
    assert NEW_KEY not in out and OLD_JWT not in out
