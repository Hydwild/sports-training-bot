"""
Токены Telegram/VK клубов: хранение в зашифрованном виде.

Раньше `Tenant.tg_token` и `Tenant.vk_token` лежали открытым текстом и
целиком показывались в форме панели оператора. Такой токен — это полный
контроль над ботом клуба: чтение всей переписки, рассылка от его имени,
смена вебхука. Он попадал в каждый дамп базы, а дамп уходит в Telegram.

Ключ ОТДЕЛЬНЫЙ (`BOT_TOKEN_ENC_KEY`), не JWT и не ключ телефонов: у них
разные сроки жизни и разные последствия компрометации. Версия ключа
хранится рядом с шифротекстом, поэтому ключ можно заменить, не потеряв
старые записи (`BOT_TOKEN_KEYRING`).

Порядок перехода — как у телефонов: сначала деплой кода, читающего оба
формата, потом `scripts/migrate_bot_tokens.py --dry-run/--apply/--verify`,
и только затем очистка plaintext-колонок отдельной миграцией.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.phones import KeyUnavailable, _fernet_key

logger = logging.getLogger("app")

KEY_V1 = "v1"          # ключ из BOT_TOKEN_ENC_KEY
KEY_LEGACY = ""        # пустая версия = значение ещё лежит открытым текстом


def _configured_keys() -> dict[str, str]:
    """Все доступные версии ключей токенов {версия: секрет}.

    Источники (поздний перекрывает ранний):
      v1 — из BOT_TOKEN_ENC_KEY (прежняя схема);
      BOT_TOKEN_KEYS    — явные неизменяемые версии `v1:secret,v2:secret`;
      BOT_TOKEN_KEYRING — прежняя связка, оставлена для совместимости.
    В отличие от телефонов, историческая версия из JWT здесь не выводится:
    токены до шифрования лежали ОТКРЫТЫМ текстом (версия пустая), а не под
    ключом из JWT."""
    from app.core.phones import _parse_keyring

    keys: dict[str, str] = {}
    penc = (settings.bot_token_enc_key or "").strip()
    if penc:
        keys[KEY_V1] = penc
    for src in (settings.bot_token_keyring, settings.bot_token_keys):
        keys.update(_parse_keyring(src))
    return keys


def key_configured() -> bool:
    return bool(_configured_keys())


def active_key_ver() -> str:
    explicit = (settings.bot_token_active_key_version or "").strip()
    return explicit or KEY_V1


def _secret_for(key_ver: str) -> str:
    keys = _configured_keys()
    if key_ver in keys:
        return keys[key_ver]
    raise KeyUnavailable(f"ключ токенов версии {key_ver!r} не задан")


def encrypt(token: str) -> tuple[str, str]:
    """(шифротекст, версия ключа). Требует настроенного ключа."""
    ver = active_key_ver()
    box = Fernet(_fernet_key(_secret_for(ver)))
    return box.encrypt(token.strip().encode()).decode(), ver


def decrypt(ciphertext: str, key_ver: str) -> str:
    """Токен или пустая строка. Пусто трактуется вызывающим кодом как
    «бот этого клуба не запускаем» — это безопаснее, чем упасть на старте
    и уронить всех остальных клиентов платформы."""
    if not ciphertext:
        return ""
    try:
        secret = _secret_for(key_ver or active_key_ver())
    except KeyUnavailable as e:
        logger.error("Токен клуба не расшифрован: %s", e)
        return ""
    try:
        return Fernet(_fernet_key(secret)).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        logger.error("Токен клуба не расшифрован: неверный ключ или данные")
        return ""


def token_of(tenant, kind: str) -> str:
    """Единая точка получения токена клуба: `kind` — 'tg' или 'vk'.

    Переходный период: если зашифрованного значения ещё нет, берём
    оставшийся plaintext. После migrate_bot_tokens --apply plaintext
    очищается, и эта ветка перестаёт срабатывать."""
    enc = getattr(tenant, f"{kind}_token_enc", "") or ""
    if enc:
        return decrypt(enc, getattr(tenant, f"{kind}_token_ver", "") or "")
    return (getattr(tenant, f"{kind}_token", "") or "").strip()


def set_token(tenant, kind: str, raw: str) -> None:
    """Записывает токен в клуб: зашифрованным, если ключ настроен.

    Без ключа падаем явно, а не сохраняем открытым текстом «по-тихому»:
    иначе новый клуб окажется менее защищённым, чем мигрировавшие."""
    raw = (raw or "").strip()
    if not raw:
        setattr(tenant, f"{kind}_token_enc", "")
        setattr(tenant, f"{kind}_token_ver", "")
        setattr(tenant, f"{kind}_token", None)
        return
    enc, ver = encrypt(raw)
    setattr(tenant, f"{kind}_token_enc", enc)
    setattr(tenant, f"{kind}_token_ver", ver)
    setattr(tenant, f"{kind}_token", None)      # plaintext не оставляем


def has_token(tenant, kind: str) -> bool:
    return bool((getattr(tenant, f"{kind}_token_enc", "") or "")
                or (getattr(tenant, f"{kind}_token", "") or ""))


def mask(tenant, kind: str) -> str:
    """Что показать оператору. Никогда не сам токен — только состояние."""
    return "настроен" if has_token(tenant, kind) else "не задан"
