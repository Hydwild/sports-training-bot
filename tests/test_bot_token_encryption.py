"""
Токены Telegram/VK клубов не хранятся и не показываются открытым текстом.

Токен бота — полный контроль над ботом клуба: чтение переписки, рассылка
от его имени, смена вебхука. Он лежал открытым текстом в базе, попадал в
каждый дамп (а дамп уходит в Telegram) и целиком выводился в форму
панели оператора.
"""
import re

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import bot_tokens
from app.main import app
from app.models.entities import Base, Tenant
from scripts import migrate_bot_tokens as mig

H = {"x-admin-token": "tok"}
TOKEN = "123456:СЕКРЕТНЫЙ-ТОКЕН-БОТА"


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


# ---------- сам механизм ----------

def test_roundtrip_and_version():
    enc, ver = bot_tokens.encrypt(TOKEN)
    assert TOKEN not in enc
    assert ver == bot_tokens.KEY_V1
    assert bot_tokens.decrypt(enc, ver) == TOKEN


def test_wrong_key_does_not_return_garbage(monkeypatch):
    enc, ver = bot_tokens.encrypt(TOKEN)
    monkeypatch.setattr(bot_tokens.settings, "bot_token_enc_key", "другой-ключ")
    monkeypatch.setattr(bot_tokens.settings, "bot_token_keyring", "")
    assert bot_tokens.decrypt(enc, ver) == ""


def test_missing_key_is_explicit(monkeypatch):
    from app.core.phones import KeyUnavailable

    monkeypatch.setattr(bot_tokens.settings, "bot_token_enc_key", "")
    monkeypatch.setattr(bot_tokens.settings, "bot_token_keyring", "")
    with pytest.raises(KeyUnavailable):
        bot_tokens.encrypt(TOKEN)


def test_keyring_reads_previous_key(monkeypatch):
    """Прежний ключ читается через реестр — но под СВОЕЙ версией.

    Замена ключа = НОВАЯ версия: старый секрет остаётся под v1, новый
    получает v2 и становится активным."""
    enc, ver = bot_tokens.encrypt(TOKEN)
    old = bot_tokens.settings.bot_token_enc_key
    monkeypatch.setattr(bot_tokens.settings, "bot_token_enc_key", "")
    monkeypatch.setattr(bot_tokens.settings, "bot_token_keys",
                        f"v1:{old},v2:новый-ключ")
    monkeypatch.setattr(bot_tokens.settings, "bot_token_active_key_version", "v2")
    bot_tokens.assert_config_valid()
    # прежний токен по-прежнему читается ключом своей версии
    assert bot_tokens.decrypt(enc, ver) == TOKEN
    # новые шифруются уже новым ключом
    enc2, ver2 = bot_tokens.encrypt(TOKEN)
    assert ver2 == "v2"
    assert bot_tokens.decrypt(enc2, ver2) == TOKEN


def test_redefining_v1_with_another_secret_rejected(monkeypatch):
    """Прежний приём «старый ключ в keyring под v1, новый в ENC_KEY» делал
    версию v1 неоднозначной: два секрета на один ярлык, под которым уже
    зашифрованы строки. Теперь это ошибка конфигурации, а не тихий выбор
    одного из секретов."""
    from app.core.keyring import KeyConfigError

    old = bot_tokens.settings.bot_token_enc_key
    monkeypatch.setattr(bot_tokens.settings, "bot_token_enc_key", "новый-ключ")
    monkeypatch.setattr(bot_tokens.settings, "bot_token_keyring", f"v1:{old}")
    with pytest.raises(KeyConfigError):
        bot_tokens.assert_config_valid()


# ---------- хранение и показ ----------

def test_token_never_stored_or_shown_in_plaintext():
    import asyncio

    with TestClient(app) as c:
        tid = c.post("/api/tenants", json={"name": "Клуб Токенов"},
                     headers=H).json()["id"]
        r = c.patch(f"/api/tenants/{tid}/tokens", headers=H,
                    json={"tg_token": "111:AAA"})
        assert r.status_code == 200
        # в ответе API токена нет
        assert "111:AAA" not in r.text

        async def stored():
            from app.db.engine import SessionLocal, engine
            await engine.dispose()
            async with SessionLocal() as s:
                t = await s.get(Tenant, tid)
                return t.tg_token, t.tg_token_enc, t.tg_token_ver

        plain, enc, ver = asyncio.run(stored())
        assert plain is None, "токен остался открытым текстом"
        assert enc and "111:AAA" not in enc
        assert bot_tokens.decrypt(enc, ver) == "111:AAA"

        # в форме оператора — только состояние
        login = c.post("/admin/platform/login", data={"token": "tok"},
                       follow_redirects=False)
        c.cookies.set("platform_token", login.cookies["platform_token"])
        form = c.get(f"/admin/platform/{tid}/edit").text
        assert "111:AAA" not in form
        assert "настроен" in form


# ---------- миграция ----------

async def _seed_plaintext(maker, token: str = TOKEN) -> int:
    async with maker() as s:
        t = Tenant(name="Старый клуб", tg_token=token)
        s.add(t)
        await s.commit()
        return t.id


async def _run(maker, monkeypatch, mode: str) -> int:
    monkeypatch.setattr(mig, "SessionLocal", maker)
    return await mig.main_async(mode)


async def test_dry_run_leaves_plaintext(maker, monkeypatch):
    tid = await _seed_plaintext(maker)
    assert await _run(maker, monkeypatch, "dry-run") == 0
    async with maker() as s:
        t = await s.get(Tenant, tid)
        assert t.tg_token == TOKEN, "пробный прогон изменил базу"


async def test_apply_encrypts_clears_and_is_idempotent(maker, monkeypatch):
    tid = await _seed_plaintext(maker)
    assert await _run(maker, monkeypatch, "verify") == 1     # есть plaintext
    assert await _run(maker, monkeypatch, "apply") == 0

    async with maker() as s:
        t = await s.get(Tenant, tid)
        assert not t.tg_token, "открытое значение не очищено"
        assert bot_tokens.token_of(t, "tg") == TOKEN

    assert await _run(maker, monkeypatch, "verify") == 0
    assert await _run(maker, monkeypatch, "apply") == 0      # повтор безвреден
    assert await _run(maker, monkeypatch, "verify") == 0


async def test_unreadable_ciphertext_blocks_apply(maker, monkeypatch):
    async with maker() as s:
        t = Tenant(name="Битый", tg_token_enc="не-шифротекст",
                   tg_token_ver="v1")
        s.add(t)
        await s.commit()
    assert await _run(maker, monkeypatch, "apply") == 2


async def test_migration_report_hides_tokens(maker, monkeypatch, capsys):
    await _seed_plaintext(maker)
    await _run(maker, monkeypatch, "dry-run")
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert re.search(r"будет перенесено:\s+1", out)
