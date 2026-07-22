"""Секреты и стабильные идентификаторы клиентских webhook."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from app.core.config import settings


def client_webhook_secret(platform: str, tenant_id: int, bot_token: str) -> str:
    master = (settings.webhook_master_secret or "").encode()
    if len(master) < 32:
        raise RuntimeError("WEBHOOK_MASTER_SECRET не задан или короче 32 символов")
    fingerprint = hashlib.sha256(bot_token.encode()).hexdigest()
    message = f"{platform}:{tenant_id}:{fingerprint}".encode()
    digest = hmac.new(master, message, hashlib.sha256).digest()
    # Только допустимые Telegram secret_token символы: A-Z a-z 0-9 _ -.
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def telegram_event_id(payload: dict[str, Any]) -> str:
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise ValueError("Telegram update без целого update_id")
    return str(update_id)


def vk_event_id(payload: dict[str, Any]) -> str:
    event_id = payload.get("event_id")
    if event_id:
        return str(event_id)
    # В старых версиях Callback API event_id может отсутствовать. Секрет не
    # включаем: его ротация не должна превращать тот же update в новый.
    stable = {key: value for key, value in payload.items() if key != "secret"}
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()
