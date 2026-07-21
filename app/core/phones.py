"""
Телефоны веб-клиентов: поиск без расшифровки и хранение в зашифрованном виде.

Зачем. Суточная копия базы уходит в Telegram (см. services/backup.py), а
номера до сих пор лежали в ней открытым текстом — да ещё и служили
идентификатором записи. Утечка одной копии раскрывала телефоны всех
клиентов всех клубов.

Как устроено:
  * поиск   — HMAC-SHA256(нормализованный номер) детерминирован, поэтому
              «найти клиента по телефону» работает без расшифровки;
  * хранение — Fernet (AES-128-CBC + HMAC): без ключа номер не прочитать.

Ключи и версии
--------------
У каждого ключа есть НЕИЗМЕНЯЕМАЯ версия. Версия пишется рядом с
шифротекстом (`key_ver`) и рядом с индексом (`index_ver`), и расшифровка
выбирает ключ ПО ВЕРСИИ, а не по текущему секрету. Это важно дважды:

  * добавление отдельного `PHONE_ENC_KEY` не должно «терять» строки,
    зашифрованные и проиндексированные выведенным из JWT ключом;
  * ротация `JWT_SECRET` не должна делать старые телефоны нечитаемыми.

Версии:
  jwt — исторический ключ, выведенный из JWT_SECRET. Чтобы он пережил
        ротацию JWT, старое значение секрета нужно заранее положить в
        PHONE_KEYRING (`jwt:<прежний JWT_SECRET>`).
  v1  — выделенный ключ из PHONE_ENC_KEY. Активная версия, если задан.

PHONE_KEYRING — связка прежних ключей в виде `ver:secret,ver:secret`.
Нужна только на время перехода: после `scripts/migrate_phone_keys.py
--apply --verify` в базе не остаётся строк старых версий, и связку можно
убрать (см. DISASTER_RECOVERY.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger("app")

KEY_JWT = "jwt"   # ключ выведен из JWT_SECRET (исторический)
KEY_V1 = "v1"     # выделенный ключ из PHONE_ENC_KEY


class KeyUnavailable(RuntimeError):
    """Ключ нужной версии не задан. Осознанно НЕ молчим: подставить другой
    ключ значит либо не найти клиента, либо создать его дубль."""


def _keyring() -> dict[str, str]:
    """{версия: секрет} из PHONE_KEYRING. Формат `ver:secret,ver:secret`."""
    raw = (settings.phone_keyring or "").strip()
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        ver, secret = chunk.split(":", 1)
        ver, secret = ver.strip(), secret.strip()
        if ver and secret:
            out[ver] = secret
    return out


def active_key_ver() -> str:
    """Версия, которой шифруются и индексируются НОВЫЕ записи."""
    return KEY_V1 if (settings.phone_enc_key or "").strip() else KEY_JWT


def _secret_for(key_ver: str) -> str:
    """Секрет конкретной версии. Никогда не подменяет версию другой."""
    ring = _keyring()
    if key_ver in ring:
        return ring[key_ver]
    if key_ver == KEY_V1:
        secret = (settings.phone_enc_key or "").strip()
        if not secret:
            raise KeyUnavailable(
                "Нужен ключ версии v1 (PHONE_ENC_KEY), но он не задан")
        return secret
    if key_ver == KEY_JWT:
        # исторический ключ. Пока JWT не ротировали, он совпадает с текущим
        # секретом; после ротации прежнее значение обязано лежать в
        # PHONE_KEYRING, иначе старые номера не прочитать
        return settings.jwt_secret
    raise KeyUnavailable(f"Неизвестная версия ключа телефонов: {key_ver!r}")


def known_key_versions() -> list[str]:
    """Версии, которыми сейчас можно читать: активная плюс связка."""
    vers = [active_key_ver()]
    for ver in _keyring():
        if ver not in vers:
            vers.append(ver)
    if KEY_JWT not in vers:
        vers.append(KEY_JWT)      # исторические строки читаются всегда
    return vers


def _fernet_key(secret: str) -> bytes:
    """Fernet требует 32 байта в urlsafe-base64. Приводим любой секрет."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def normalize(phone: str) -> str:
    """Только цифры: +7 900 000-00-00 и 79000000000 — один человек."""
    return "".join(c for c in phone if c.isdigit())


def phone_index(phone: str, key_ver: str | None = None) -> str:
    """Детерминированный индекс для поиска. Это НЕ хеш пароля: номеров мало
    и они перебираемы, поэтому индекс держится на секретном ключе — без
    него по дампу номер не восстановить перебором.

    Индекс версионирован: при смене ключа старые строки продолжают
    находиться по индексу своей версии (см. index_candidates)."""
    ver = key_ver or active_key_ver()
    digits = normalize(phone)
    return hmac.new(_secret_for(ver).encode(), digits.encode(),
                    hashlib.sha256).hexdigest()


def index_candidates(phone: str) -> list[tuple[str, str]]:
    """[(версия, индекс)] — активная версия первой, затем читаемые старые.

    Поиск обязан проверять их все: иначе после добавления нового ключа
    существующий клиент «пропадёт» и создастся его дубль с тем же номером."""
    out: list[tuple[str, str]] = []
    for ver in known_key_versions():
        try:
            out.append((ver, phone_index(phone, ver)))
        except KeyUnavailable:
            # ключ этой версии не задан — молча пропускаем: строки такой
            # версии просто не найдутся, но чужие мы не «переиндексируем»
            logger.warning("Ключ телефонов версии %s недоступен — строки "
                           "этой версии сейчас не ищутся", ver)
    return out


def encrypt(phone: str, key_ver: str | None = None) -> tuple[str, str]:
    """(шифротекст, версия ключа)."""
    ver = key_ver or active_key_ver()
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
        secret = _secret_for(key_ver)
    except KeyUnavailable:
        logger.warning("Нет ключа версии %s — телефон не показан", key_ver)
        return ""
    try:
        return Fernet(_fernet_key(secret)).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""
