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
from app.core.keyring import (
    KeyConfigError,
    Source,
    build_registry,
    parse_keyring,
    parse_versions,
)

logger = logging.getLogger("app")

KEY_JWT = "jwt"   # исторический ключ, выведенный из JWT_SECRET
KEY_V1 = "v1"     # выделенный ключ из PHONE_ENC_KEY (обратная совместимость)

# наружу переэкспортируем — на них ловят вызывающие
__all__ = ["KeyUnavailable", "KeyConfigError"]


class KeyUnavailable(RuntimeError):
    """Ключ нужной версии недоступен ИЛИ его секрет не проходит проверку по
    данным. Осознанно НЕ молчим: подставить другой ключ значит либо не найти
    клиента, либо создать его дубль."""


# для обратной совместимости с прежними вызовами внутри модуля/тестов
_parse_keyring = parse_keyring
_parse_versions = parse_versions


def _sources() -> list[Source]:
    """Источники версий ключей телефонов, от неявных к явным.

    jwt/v1 — значения по умолчанию (их явные keyring перекрывают без
    конфликта, это механизм ротации). PHONE_KEYRING/PHONE_KEYS — явные."""
    srcs = [Source("JWT_SECRET→jwt", {KEY_JWT: settings.jwt_secret},
                   implicit=True)]
    penc = (settings.phone_enc_key or "").strip()
    if penc:
        srcs.append(Source("PHONE_ENC_KEY→v1", {KEY_V1: penc}, implicit=True))
    srcs.append(Source("PHONE_KEYRING", parse_keyring(settings.phone_keyring)))
    srcs.append(Source("PHONE_KEYS", parse_keyring(settings.phone_keys)))
    return srcs


def _configured_keys() -> dict[str, str]:
    """{версия: секрет} — строгий реестр. Бросает KeyConfigError при
    конфликте секретов одной версии или недопустимой метке."""
    return build_registry("phone", _sources())


def active_key_ver() -> str:
    """Версия, которой шифруются и индексируются НОВЫЕ записи."""
    explicit = (settings.phone_active_key_version or "").strip()
    if explicit:
        return explicit
    # обратная совместимость: v1 при заданном PHONE_ENC_KEY, иначе jwt
    return KEY_V1 if (settings.phone_enc_key or "").strip() else KEY_JWT


def assert_config_valid() -> None:
    """Проверка конфигурации ключей БЕЗ обращения к базе: конфликты меток и
    существование активной версии. Бросает KeyConfigError."""
    keys = _configured_keys()   # ловит конфликты/метки
    active = active_key_ver()
    from app.core.keyring import VERSION_RE
    if not VERSION_RE.match(active):
        raise KeyConfigError(
            f"phone: активная версия {active!r} недопустима (до 8 "
            "латинских букв/цифр)")
    if active not in keys:
        raise KeyConfigError(
            f"phone: активная версия {active!r} отсутствует в реестре ключей")


def _secret_for(key_ver: str) -> str:
    """Секрет конкретной версии. Никогда не подменяет версию другой."""
    keys = _configured_keys()
    if key_ver in keys:
        return keys[key_ver]
    raise KeyUnavailable(f"ключ телефонов версии {key_ver!r} недоступен")


def read_versions() -> list[str]:
    """Версии, которые проверяем при поиске клиента: активная, затем
    объявленные операторам legacy, затем историческая jwt.

    Это КОНФИГУРАЦИОННЫЙ список. Реально используемые версии из БД —
    отдельный источник истины (см. db_used_versions в репозитории)."""
    vers = [active_key_ver()]
    for ver in parse_versions(settings.phone_legacy_versions):
        if ver not in vers:
            vers.append(ver)
    if KEY_JWT not in vers:
        vers.append(KEY_JWT)
    return vers


def known_key_versions() -> list[str]:
    """Совместимый псевдоним read_versions() (использовался ранее)."""
    return read_versions()


def missing_read_versions() -> list[str]:
    """Из ОБЪЯВЛЕННЫХ при поиске версий — те, чьего ключа сейчас нет.

    Это только про конфигурацию (PHONE_LEGACY_VERSIONS). Версии, реально
    присутствующие в БД, проверяются отдельно — см. verify_row_secret и
    репозиторий: отсутствие версии в PHONE_LEGACY_VERSIONS не даёт
    проигнорировать версию, которая реально есть в данных."""
    keys = _configured_keys()
    return [v for v in read_versions() if v not in keys]


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


def verify_row_secret(key_ver: str, index_ver: str, phone_enc: str,
                      phone_index_stored: str) -> None:
    """Доказывает, что настроенные секреты версий key_ver/index_ver — ПРАВИЛЬНЫЕ
    для этой строки: расшифровывает телефон ключом key_ver и заново считает
    индекс ключом index_ver, сверяя с сохранённым.

    Именно так ловится подменённый секрет под формально существующей версией
    (например jwt после ротации JWT_SECRET без сохранения старого ключа):
    расшифровка даст мусор/пусто, а индекс не совпадёт. Бросает
    KeyUnavailable без раскрытия секретов и телефонов."""
    # секреты должны существовать (иначе версия недоступна вовсе)
    try:
        enc_secret = _secret_for(key_ver)
        _secret_for(index_ver)
    except KeyUnavailable:
        raise
    try:
        digits = Fernet(_fernet_key(enc_secret)).decrypt(
            phone_enc.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        raise KeyUnavailable(
            f"секрет ключа телефонов версии {key_ver!r} не расшифровывает "
            "существующие данные — вероятно, ключ подменён") from None
    if not digits.isdigit():
        raise KeyUnavailable(
            f"ключ телефонов версии {key_ver!r} даёт неверную расшифровку")
    if phone_index(digits, index_ver) != phone_index_stored:
        raise KeyUnavailable(
            f"секрет ключа телефонов версии {index_ver!r} не воспроизводит "
            "сохранённый индекс — вероятно, ключ подменён")
