"""Безопасное переключение клиентских ботов polling ↔ webhook."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.vk_api import VKBot, VKTransport
from app.core import bot_tokens
from app.core.config import settings
from app.models.entities import Tenant
from app.services.webhook_security import client_webhook_secret

logger = logging.getLogger("delivery_modes")


def _value(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _webhook_url(path: str) -> str:
    url = settings.public_url(path)
    if not url.lower().startswith("https://"):
        raise RuntimeError("Для webhook PUBLIC_BASE_URL должен начинаться с https://")
    return url


async def _reload_bots() -> bool:
    from app.bots import telegram, vk
    try:
        await telegram.reload_client_bots()
        await vk.reload_client_bots()
        return True
    except Exception:
        # Внешняя регистрация и БД уже согласованы. Не откатываем их из-за
        # локального hot-reload: после рестарта реестры прочитаются из БД.
        logger.exception("Hot-reload после переключения не удался")
        return False


async def set_telegram_mode(
    session: AsyncSession, tenant: Tenant, mode: str,
) -> dict[str, Any]:
    mode = (mode or "").lower()
    if mode not in ("polling", "webhook"):
        raise ValueError("Telegram mode должен быть polling или webhook")
    token = bot_tokens.token_of(tenant, "tg")
    if not token:
        raise ValueError("У клуба не задан Telegram-токен")
    if token == settings.tg_token:
        raise ValueError("Этот токен используется глобальным Telegram-ботом")
    if mode == "webhook":
        secret = client_webhook_secret("tg", tenant.id, token)
        url = _webhook_url(f"/webhook/telegram/{tenant.id}")
    else:
        secret = ""
        url = ""

    bot = Bot(token=token)
    try:
        if mode == "webhook":
            ok = await bot.set_webhook(
                url=url,
                secret_token=secret,
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
            )
        else:
            ok = await bot.delete_webhook(drop_pending_updates=False)
        if not ok:
            raise RuntimeError("Telegram API не подтвердил смену режима")
    finally:
        await bot.session.close()

    tenant.tg_delivery_mode = mode
    await session.commit()
    await _reload_bots()
    return {"platform": "tg", "mode": mode, "url": url or None}


async def _matching_vk_servers(api, group_id: int, url: str) -> list[int]:
    response = await api.groups.get_callback_servers(group_id=group_id)
    items = _value(response, "items", []) or []
    result: list[int] = []
    for item in items:
        if str(_value(item, "url", "")).rstrip("/") == url.rstrip("/"):
            server_id = _value(item, "id") or _value(item, "server_id")
            if server_id is not None:
                result.append(int(server_id))
    return result


async def _delete_vk_servers(api, group_id: int, server_ids: list[int]) -> None:
    for server_id in server_ids:
        await api.groups.delete_callback_server(
            group_id=group_id, server_id=server_id,
        )


async def set_vk_mode(
    session: AsyncSession, tenant: Tenant, mode: str,
) -> dict[str, Any]:
    mode = (mode or "").lower()
    if mode not in ("longpoll", "callback"):
        raise ValueError("VK mode должен быть longpoll или callback")
    if mode == "longpoll" and not settings.run_vk_polling:
        raise RuntimeError("Для VK longpoll задайте RUN_VK_POLLING=true")
    token = bot_tokens.token_of(tenant, "vk")
    if not token:
        raise ValueError("У клуба не задан VK-токен")
    if token == settings.vk_token:
        raise ValueError("Этот токен используется глобальным VK-ботом")

    url = _webhook_url("/webhook/vk")
    transport = VKTransport()
    bot = VKBot(token, transport)
    server_id: int | None = None
    old_mode = tenant.vk_delivery_mode
    old_code = tenant.vk_confirmation_code
    old_group_id = tenant.vk_group_id
    try:
        group_id = await bot.resolve_group_id()
        existing = await _matching_vk_servers(bot.api, group_id, url)

        if mode == "longpoll":
            await _delete_vk_servers(bot.api, group_id, existing)
            tenant.vk_group_id = group_id
            tenant.vk_delivery_mode = "longpoll"
            tenant.vk_confirmation_code = ""
            await session.commit()
            await _reload_bots()
            return {"platform": "vk", "mode": mode, "url": None,
                    "removed_servers": len(existing)}

        secret = client_webhook_secret("vk", tenant.id, token)
        confirmation = await bot.api.groups.get_callback_confirmation_code(
            group_id=group_id,
        )
        code = str(_value(confirmation, "code", ""))
        if not code:
            raise RuntimeError("VK не вернул confirmation code")

        # Endpoint должен уметь подтвердить адрес до addCallbackServer.
        tenant.vk_group_id = group_id
        tenant.vk_delivery_mode = "callback"
        tenant.vk_confirmation_code = code
        await session.commit()

        await _delete_vk_servers(bot.api, group_id, existing)
        added = await bot.api.groups.add_callback_server(
            group_id=group_id,
            url=url,
            title="sports-bot",
            secret=secret,
        )
        server_id = int(_value(added, "server_id"))
        configured = await bot.api.groups.set_callback_settings(
            group_id=group_id,
            server_id=server_id,
            api_version="5.199",
            message_new=1,
            message_event=1,
        )
        if configured != 1:
            raise RuntimeError("VK не подтвердил настройки Callback API")
        await _reload_bots()
        return {"platform": "vk", "mode": mode, "url": url,
                "server_id": server_id}
    except Exception:
        if server_id is not None and bot.group_id is not None:
            try:
                await _delete_vk_servers(bot.api, bot.group_id, [server_id])
            except Exception:
                logger.exception("Не удалось удалить незавершённый VK callback server")
        tenant.vk_delivery_mode = old_mode
        tenant.vk_confirmation_code = old_code
        tenant.vk_group_id = old_group_id
        await session.commit()
        raise
    finally:
        await transport.close()
