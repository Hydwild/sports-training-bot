"""
Телефоны веб-клиентов: поиск без расшифровки и хранение в зашифрованном виде.

Зачем. Суточная копия базы уходит в Telegram (см. services/backup.py), а
номера до сих пор лежали в ней открытым текстом — да ещё и служили
идентификатором записи. Утечка одной копии раскрывала телефоны всех
клиентов всех клубов.

Как устроено:
  * поиск   — HMAC-SHA256(нормализованный номер) детерминирован, поэтому
              «найти запись по телефону» работает без расшифровки;
  * хранение — Fernet (AES-128-CBC + HMAC): без ключа номер не прочитать.

Ключ берётся из PHONE_ENC_KEY, а если он не задан — выводится из
JWT_SECRET. Версия ключа пишется рядом с шифротекстом (key_ver), поэтому
отдельный PHONE_ENC_KEY можно добавить позже: старые записи продолжат
читаться выведенным ключом, новые пойдут на выделенном.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

KEY_ENV = "env"   # номер зашифрован ключом из PHONE_ENC_KEY
KEY_JWT = "jwt"   # ключ выведен из JWT_SECRET (PHONE_ENC_KEY не задан)


def _fernet_key(secret: str) -> bytes:
    """Fernet требует 32 байта в urlsafe-base64. Приводим любой секрет."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def current_key_ver() -> str:
    return KEY_ENV if (settings.phone_enc_key or "").strip() else KEY_JWT


def _secret_for(key_ver: str) -> str:
    if key_ver == KEY_ENV:
        return (settings.phone_enc_key or "").strip()
    return settings.jwt_secret


def normalize(phone: str) -> str:
    """Только цифры: +7 900 000-00-00 и 79000000000 — один человек."""
    return "".join(c for c in phone if c.isdigit())


def phone_index(phone: str) -> str:
    """Детерминированный индекс для поиска. Это НЕ хеш пароля: номеров мало
    и они перебираемы, поэтому индекс держится на секретном ключе —
    без него по дампу номер не восстановить перебором."""
    digits = normalize(phone)
    return hmac.new(_secret_for(current_key_ver()).encode(), digits.encode(),
                    hashlib.sha256).hexdigest()


def encrypt(phone: str) -> tuple[str, str]:
    """(шифротекст, версия ключа)."""
    ver = current_key_ver()
    token = Fernet(_fernet_key(_secret_for(ver))).encrypt(
        normalize(phone).encode())
    return token.decode(), ver


def decrypt(token: str, key_ver: str = KEY_JWT) -> str:
    """Номер или пустая строка, если расшифровать нечем.

    Пустая строка вместо исключения — намеренно: сменили ключ или потеряли
    его, карточка участника всё равно должна открыться, просто без номера."""
    if not token:
        return ""
    try:
        return Fernet(_fernet_key(_secret_for(key_ver))).decrypt(
            token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""
