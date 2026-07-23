"""
Консоль владельца площадки в Telegram.

Главное здесь — не удобство, а изоляция: команды видит только владелец и
только в личном чате, а секреты (токены ботов) в переписку не попадают ни
при каких обстоятельствах. История чата Telegram вечна и лежит на всех
устройствах, поэтому утечка туда необратима.
"""
import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bots import platform_console as pc
from app.core.config import settings
from app.models.entities import Base, Tenant

OWNER = 555001
STRANGER = 999002


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pc, "SessionLocal", maker)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", OWNER)


class _Chat:
    def __init__(self, kind="private"):
        self.type = kind


class _User:
    def __init__(self, uid):
        self.id = uid


class _Msg:
    """Минимальное сообщение aiogram: важны только поля, которые читаем."""

    def __init__(self, text, uid=OWNER, chat="private"):
        self.text = text
        self.from_user = _User(uid)
        self.chat = _Chat(chat)
        self.answers: list[str] = []
        self.documents: list[tuple[str, bytes, str]] = []

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def answer_document(self, doc, caption="", **kw):
        self.documents.append((doc.filename, doc.data, caption))

    @property
    def last(self) -> str:
        return self.answers[-1] if self.answers else ""


async def _seed(maker) -> None:
    async with maker() as s:
        today = dt.date.today()
        s.add(Tenant(name="Живой клуб",
                     paid_until=(today + dt.timedelta(days=20)).isoformat()))
        s.add(Tenant(name="Просроченный",
                     paid_until=(today - dt.timedelta(days=4)).isoformat()))
        s.add(Tenant(name="Бессрочный", paid_until=""))
        await s.commit()


# ---------- доступ ----------

async def test_stranger_is_not_recognised():
    """Для постороннего команды не существует: фильтр не совпадает, и
    событие уходит дальше — мы даже не намекаем, что консоль есть."""
    assert await pc.OwnerOnly()(_Msg("/clients", uid=STRANGER)) is False


async def test_owner_in_group_chat_is_rejected():
    """В группе бот работает для участников клуба — панель оператора там
    увидели бы посторонние."""
    assert await pc.OwnerOnly()(_Msg("/clients", chat="group")) is False


async def test_owner_in_private_chat_is_allowed():
    assert await pc.OwnerOnly()(_Msg("/clients")) is True


async def test_console_disabled_when_owner_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "platform_owner_tg_id", 0)
    assert await pc.OwnerOnly()(_Msg("/clients")) is False


# ---------- обзор ----------

async def test_clients_list_shows_rent_and_sorts_burning_first(db):
    await _seed(db)
    m = _Msg("/clients")
    await pc.cmd_clients(m)
    text = m.last
    assert "Клиенты платформы (3)" in text
    assert "ИСТЕКЛА 4 дн. назад" in text
    assert "20 дн." in text and "без ограничения" in text
    # просроченный должен идти раньше бессрочного
    assert text.index("Просроченный") < text.index("Бессрочный")


async def test_client_card_reports_load(db):
    await _seed(db)
    m = _Msg("/client 1")
    await pc.cmd_client(m)
    text = m.last
    assert "Живой клуб" in text
    assert "участников всего" in text and "за 7 дней" in text


async def test_client_card_for_missing_club(db):
    await _seed(db)
    m = _Msg("/client 999")
    await pc.cmd_client(m)
    assert "нет" in m.last.lower()


# ---------- секреты не утекают ----------

async def test_bot_token_never_appears_in_any_output(db):
    """Ключевой инвариант: токен клиента не попадает в переписку."""
    from app.core import bot_tokens

    token = "123456:СЕКРЕТНЫЙ-ТОКЕН-КЛИЕНТА"
    async with db() as s:
        t = Tenant(name="С ботом", paid_until="")
        bot_tokens.set_token(t, "tg", token)
        s.add(t)
        await s.commit()

    for text, handler in (("/clients", pc.cmd_clients),
                          ("/client 1", pc.cmd_client)):
        m = _Msg(text)
        await handler(m)
        assert token not in m.last, f"{text}: токен утёк в чат"
        assert "TG" in m.last, f"{text}: состояние бота должно быть видно"


async def test_newclub_refuses_to_take_token_in_chat(db):
    m = _Msg("/newclub Новый клуб")
    await pc.cmd_newclub(m)
    text = m.last
    assert "заведён" in text
    # прямо предупреждаем не присылать токен в переписку
    assert "токен" in text.lower()


# ---------- аренда ----------

async def test_extend_adds_days_from_current_end(db):
    await _seed(db)
    m = _Msg("/extend 1 30")
    await pc.cmd_extend(m)
    expected = (dt.date.today() + dt.timedelta(days=50)).isoformat()
    assert expected in m.last, m.last


async def test_extend_of_expired_club_starts_from_today(db):
    """Оплата задним числом не должна «сгорать»: продлеваем от сегодня."""
    await _seed(db)
    m = _Msg("/extend 2 10")
    await pc.cmd_extend(m)
    expected = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    assert expected in m.last, m.last


@pytest.mark.parametrize("cmd", ["/extend", "/extend 1", "/extend x 5",
                                 "/extend 1 0", "/extend 1 400"])
async def test_extend_rejects_bad_input(db, cmd):
    await _seed(db)
    m = _Msg(cmd)
    await pc.cmd_extend(m)
    assert "польз" in m.last.lower() or "1–366" in m.last


# ---------- логи ----------

async def test_logs_refuse_plaintext_without_key(db, monkeypatch, tmp_path):
    """Журнал содержит имена и id — без ключа шифрования не отправляем."""
    monkeypatch.setattr(pc.settings, "log_dir", str(tmp_path))
    (tmp_path / "errors.log").write_bytes("ERROR user=12345 Иванов\n".encode())
    monkeypatch.setattr("app.services.backup.encryption_enabled",
                        lambda: False)
    m = _Msg("/logs")
    await pc.cmd_logs(m)
    assert not m.documents, "журнал ушёл незашифрованным"
    assert "BACKUP_ENC_KEY" in m.last


async def test_logs_are_sent_encrypted(db, monkeypatch, tmp_path):
    secret = b"ERROR user=12345 Ivanov failed"
    monkeypatch.setattr(pc.settings, "log_dir", str(tmp_path))
    (tmp_path / "errors.log").write_bytes(secret)
    monkeypatch.setattr(pc.settings, "backup_enc_key", "тестовый-ключ-логов")

    m = _Msg("/logs")
    await pc.cmd_logs(m)
    assert m.documents, "журнал не отправлен"
    name, blob, _caption = m.documents[0]
    assert name.endswith(".enc")
    assert secret not in blob, "содержимое журнала ушло открытым текстом"

    from app.services.backup import decrypt_backup
    assert decrypt_backup(blob) == secret      # но владелец его прочитает


async def test_logs_empty_is_reported(db, monkeypatch, tmp_path):
    monkeypatch.setattr(pc.settings, "log_dir", str(tmp_path))
    monkeypatch.setattr(pc.settings, "backup_enc_key", "тестовый-ключ-логов")
    m = _Msg("/logs")
    await pc.cmd_logs(m)
    assert not m.documents
    assert "пуст" in m.last


# ---------- почему бот клуба молчит ----------

class _Info:
    def __init__(self, url, pending=0, err=None):
        self.url, self.pending_update_count, self.last_error_message = \
            url, pending, err


class _Bot:
    """Заглушка Bot API: отдаёт заранее заданный getWebhookInfo."""
    def __init__(self, info):
        self._info = info
        self.session = type("S", (), {"close": _noop})()

    async def get_webhook_info(self):
        return self._info


async def _noop():
    return None


def _webhook_tenant(tid=7, name="Клуб вебхук"):
    t = Tenant(name=name)
    t.id = tid
    t.tg_delivery_mode = "webhook"
    return t


def _expected_url(tenant_id: int) -> str:
    """Адрес, который ДОЛЖЕН быть прописан в Telegram. Берём из тех же
    настроек, что и код: тест не должен зависеть от того, какой
    PUBLIC_BASE_URL достался прогону."""
    return pc.settings.public_url(f"/webhook/telegram/{tenant_id}")


def _patch(monkeypatch, tenants, info):
    monkeypatch.setattr("app.core.bot_tokens.token_of", lambda t, k: "111:AAA")

    class _G:
        def __init__(self, _s): pass
        async def list_tenants(self): return tenants

    monkeypatch.setattr("app.repositories.repo.GlobalRepository", _G)
    monkeypatch.setattr("aiogram.Bot", lambda token: _Bot(info))


async def test_webhook_mismatch_is_reported(db, monkeypatch):
    """Смена домена — самый коварный случай: бот молчит, а в логах пусто,
    потому что запрос до нас вообще не доходит."""
    t = _webhook_tenant()
    _patch(monkeypatch, [t], _Info("https://old.example/webhook/telegram/7"))
    m = _Msg("/webhooks")
    await pc.cmd_webhooks(m)
    assert "old.example" in m.last, "не показан адрес, куда шлёт Telegram"
    assert _expected_url(7) in m.last, "не показан ожидаемый адрес"
    assert "❌" in m.last
    assert "Перевести на webhook" in m.last, "нет подсказки, что делать"


async def test_matching_webhook_is_ok(db, monkeypatch):
    t = _webhook_tenant()
    _patch(monkeypatch, [t], _Info(_expected_url(7)))
    m = _Msg("/webhooks")
    await pc.cmd_webhooks(m)
    assert "✅" in m.last and "❌" not in m.last


async def test_last_error_from_telegram_is_shown(db, monkeypatch):
    """Адрес верный, но Telegram не достучался — это тоже причина молчания."""
    t = _webhook_tenant()
    _patch(monkeypatch, [t], _Info(_expected_url(7), 12,
                                   "Connection timed out"))
    m = _Msg("/webhooks")
    await pc.cmd_webhooks(m)
    assert "Connection timed out" in m.last
    assert "12" in m.last, "не показано, сколько обновлений скопилось"


async def test_polling_clubs_are_not_listed(db, monkeypatch):
    t = _webhook_tenant()
    t.tg_delivery_mode = "polling"
    _patch(monkeypatch, [t], _Info(""))
    m = _Msg("/webhooks")
    await pc.cmd_webhooks(m)
    assert "polling" in m.last.lower()


async def test_command_is_owner_only():
    assert await pc.OwnerOnly()(_Msg("/webhooks", uid=STRANGER)) is False
