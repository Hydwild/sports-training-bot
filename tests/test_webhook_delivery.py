import re
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.entities import Base, InboundEvent, Tenant
from app.repositories.repo import GlobalRepository, TenantRepository
from app.services import inbound, tasks
from app.services.webhook_security import (
    client_webhook_secret,
    telegram_event_id,
    vk_event_id,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    result = async_sessionmaker(engine, expire_on_commit=False)
    yield result
    await engine.dispose()


def test_webhook_secrets_are_stable_distinct_and_header_safe(monkeypatch):
    from app.services import webhook_security
    monkeypatch.setattr(webhook_security.settings, "webhook_master_secret", "m" * 48)
    one = client_webhook_secret("tg", 1, "123:token")
    assert one == client_webhook_secret("tg", 1, "123:token")
    assert one != client_webhook_secret("tg", 2, "123:token")
    assert one != client_webhook_secret("vk", 1, "123:token")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", one)


def test_global_telegram_webhook_fails_closed_without_secret(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app, settings
    monkeypatch.setattr(settings, "tg_webhook_secret", "")
    response = TestClient(app).post(
        "/webhook/telegram", json={"update_id": 1},
    )
    assert response.status_code == 403


def test_tenant_telegram_webhook_authenticates_and_deduplicates(monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient
    from sqlalchemy import func

    from app.db.engine import SessionLocal
    from app.main import app
    from app.services import webhook_security

    token = "987654:WEBHOOKTEST"
    monkeypatch.setattr(webhook_security.settings, "webhook_master_secret", "s" * 48)
    with TestClient(app) as client:
        tenant_id = client.post(
            "/api/tenants", json={"name": "Webhook endpoint"},
            headers={"x-admin-token": "tok"},
        ).json()["id"]
        response = client.patch(
            f"/api/tenants/{tenant_id}/tokens", json={"tg_token": token},
            headers={"x-admin-token": "tok"},
        )
        assert response.status_code == 200

        async def set_mode_and_count(mode: str) -> int:
            async with SessionLocal() as session:
                tenant = await session.get(Tenant, tenant_id)
                tenant.tg_delivery_mode = mode
                await session.commit()
                return int((await session.execute(
                    select(func.count()).select_from(InboundEvent).where(
                        InboundEvent.tenant_id == tenant_id,
                    )
                )).scalar_one())

        before = asyncio.run(set_mode_and_count("webhook"))
        path = f"/webhook/telegram/{tenant_id}"
        payload = {"update_id": 123456789, "message": {"text": "/start"}}
        assert client.post(path, json=payload).status_code == 403
        secret = client_webhook_secret("tg", tenant_id, token)
        headers = {"x-telegram-bot-api-secret-token": secret}
        assert client.post(path, json=payload, headers=headers).status_code == 200
        assert client.post(path, json=payload, headers=headers).status_code == 200
        assert asyncio.run(set_mode_and_count("webhook")) == before + 1
        asyncio.run(set_mode_and_count("polling"))


def test_webhook_event_ids_are_deterministic_and_ignore_vk_secret():
    assert telegram_event_id({"update_id": 42}) == "42"
    first = vk_event_id({"type": "message_new", "object": {"x": 1},
                         "secret": "old"})
    second = vk_event_id({"object": {"x": 1}, "type": "message_new",
                          "secret": "new"})
    assert first == second
    assert vk_event_id({"event_id": "abc", "secret": "x"}) == "abc"


def test_runtime_has_no_vkbottle_dependency_or_import():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "vkbottle" not in requirements.lower()
    for source in (ROOT / "app").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "import vkbottle" not in text
        assert "from vkbottle" not in text


async def test_light_vk_client_maps_methods_and_keyboard_without_models():
    import json

    from app.bots.vk_api import VKAPI, _param
    from app.bots.vk_keyboard import Callback, Keyboard, KeyboardButtonColor

    calls = []

    class Transport:
        async def api_call(self, token, method, **params):
            calls.append((token, method, params))
            return {"ok": 1}

    api = VKAPI("token", Transport())
    await api.groups.get_callback_confirmation_code(group_id=7)
    assert calls == [
        ("token", "groups.getCallbackConfirmationCode", {"group_id": 7})
    ]
    assert _param([1, 2]) == "1,2"
    assert _param(["screen_name", "photo_200"]) == "screen_name,photo_200"

    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("Записаться", payload={"a": "su", "tid": 5}),
                 color=KeyboardButtonColor.POSITIVE)
    raw = json.loads(keyboard.get_json())
    assert raw["inline"] is True
    button = raw["buttons"][0][0]
    assert button["action"]["type"] == "callback"
    assert json.loads(button["action"]["payload"]) == {"a": "su", "tid": 5}


async def test_inbound_is_durable_deduplicated_and_clears_payload(
    monkeypatch, maker,
):
    monkeypatch.setattr(inbound, "SessionLocal", maker)
    async with maker() as session:
        tenant = await GlobalRepository(session).create_tenant(name="Inbox")
        await session.commit()
        tenant_id = tenant.id

    payload = {"update_id": 100, "message": {"text": "секретный текст"}}
    assert await inbound.ingest(
        platform="tg", tenant_id=tenant_id,
        external_event_id="100", payload=payload,
    ) is True
    assert await inbound.ingest(
        platform="tg", tenant_id=tenant_id,
        external_event_id="100", payload=payload,
    ) is False

    event = await inbound._claim_one()
    assert event is not None and event.payload == payload
    await inbound._mark_done(event.id)
    async with maker() as session:
        rows = list((await session.execute(select(InboundEvent))).scalars())
        assert len(rows) == 1
        assert rows[0].status == "done"
        assert rows[0].payload == "{}"


async def test_outbox_wakes_only_after_commit(maker):
    tasks._outbox_wakeup.clear()
    async with maker() as session:
        tenant = await GlobalRepository(session).create_tenant(name="Wake")
        await session.commit()
        tenant_id = tenant.id
        await TenantRepository(session, tenant_id).enqueue("tg", 1, "hello")
        assert not tasks._outbox_wakeup.is_set()
        await session.rollback()
        assert not tasks._outbox_wakeup.is_set()

    async with maker() as session:
        await TenantRepository(session, tenant_id).enqueue("tg", 1, "hello")
        assert not tasks._outbox_wakeup.is_set()
        await session.commit()
        assert tasks._outbox_wakeup.is_set()
    tasks._outbox_wakeup.clear()


async def test_telegram_mode_registers_webhook_before_saving(
    monkeypatch, maker,
):
    from app.services import delivery_modes, webhook_security

    calls = []

    class FakeSession:
        async def close(self):
            calls.append(("close",))

    class FakeBot:
        def __init__(self, token):
            self.token = token
            self.session = FakeSession()

        async def set_webhook(self, **kwargs):
            calls.append(("set", kwargs))
            return True

        async def delete_webhook(self, **kwargs):
            calls.append(("delete", kwargs))
            return True

    async def no_reload():
        return True

    monkeypatch.setattr(delivery_modes, "Bot", FakeBot)
    monkeypatch.setattr(delivery_modes, "_reload_bots", no_reload)
    monkeypatch.setattr(delivery_modes.settings, "public_base_url", "https://bot.test")
    monkeypatch.setattr(webhook_security.settings, "webhook_master_secret", "w" * 48)

    async with maker() as session:
        tenant = Tenant(name="TG", tg_token="123456:ABC")
        session.add(tenant)
        await session.commit()
        result = await delivery_modes.set_telegram_mode(session, tenant, "webhook")
        assert tenant.tg_delivery_mode == "webhook"
        assert result["url"] == f"https://bot.test/webhook/telegram/{tenant.id}"
    assert calls[0][0] == "set"
    assert calls[0][1]["drop_pending_updates"] is False
    assert calls[0][1]["secret_token"]


async def test_global_telegram_webhook_is_registered_automatically(monkeypatch):
    from app.bots import telegram

    calls = []

    class FakeBot:
        async def set_webhook(self, **kwargs):
            calls.append(("set", kwargs))
            return True

        async def delete_webhook(self, **kwargs):
            calls.append(("delete", kwargs))
            return True

    monkeypatch.setattr(telegram, "_bot", FakeBot())
    monkeypatch.setattr(telegram.settings, "tg_mode", "webhook")
    monkeypatch.setattr(telegram.settings, "tg_webhook_secret", "s" * 32)
    monkeypatch.setattr(
        telegram.settings, "public_base_url", "https://bot.example.com/",
    )

    await telegram.configure_global_delivery()

    assert calls == [("set", {
        "url": "https://bot.example.com/webhook/telegram",
        "secret_token": "s" * 32,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })]


async def test_global_telegram_polling_deletes_webhook(monkeypatch):
    from app.bots import telegram

    calls = []

    class FakeBot:
        async def delete_webhook(self, **kwargs):
            calls.append(kwargs)
            return True

    monkeypatch.setattr(telegram, "_bot", FakeBot())
    monkeypatch.setattr(telegram.settings, "tg_mode", "polling")

    await telegram.configure_global_delivery()

    assert calls == [{"drop_pending_updates": False}]


async def test_vk_mode_registers_callback_and_persists_group(monkeypatch, maker):
    from app.services import delivery_modes, webhook_security

    calls = []

    class Groups:
        async def get_callback_servers(self, **kwargs):
            return {"items": []}

        async def get_callback_confirmation_code(self, **kwargs):
            return {"code": "confirm-me"}

        async def add_callback_server(self, **kwargs):
            calls.append(("add", kwargs))
            return {"server_id": 91}

        async def set_callback_settings(self, **kwargs):
            calls.append(("settings", kwargs))
            return 1

        async def delete_callback_server(self, **kwargs):
            calls.append(("delete", kwargs))

    class FakeTransport:
        async def close(self):
            calls.append(("close", {}))

    class FakeVKBot:
        def __init__(self, token, transport):
            self.api = type("API", (), {"groups": Groups()})()
            self.group_id = None

        async def resolve_group_id(self):
            self.group_id = 777
            return 777

    async def no_reload():
        return True

    monkeypatch.setattr(delivery_modes, "VKTransport", FakeTransport)
    monkeypatch.setattr(delivery_modes, "VKBot", FakeVKBot)
    monkeypatch.setattr(delivery_modes, "_reload_bots", no_reload)
    monkeypatch.setattr(delivery_modes.settings, "public_base_url", "https://bot.test")
    monkeypatch.setattr(webhook_security.settings, "webhook_master_secret", "v" * 48)

    async with maker() as session:
        tenant = Tenant(name="VK", vk_token="vk-token")
        session.add(tenant)
        await session.commit()
        result = await delivery_modes.set_vk_mode(session, tenant, "callback")
        assert tenant.vk_delivery_mode == "callback"
        assert tenant.vk_confirmation_code == "confirm-me"
        assert tenant.vk_group_id == 777
        assert result["server_id"] == 91
    assert [name for name, _ in calls] == ["add", "settings", "close"]
    assert calls[0][1]["secret"]
    assert calls[1][1]["message_new"] == 1
    assert calls[1][1]["message_event"] == 1
