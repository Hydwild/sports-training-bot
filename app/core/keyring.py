"""
Строгий реестр версионированных ключей — общий для телефонов и токенов ботов.

Версии объявлены НЕИЗМЕНЯЕМЫМИ: под конкретной меткой (`jwt`, `v1`, `v2`)
зашифрованы конкретные строки в базе. Поэтому:

  * одна версия с ОДНИМ секретом в нескольких источниках — допустимо;
  * одна версия с РАЗНЫМИ секретами в явных источниках — ошибка
    конфигурации (мы бы молча взяли один из них и потеряли/задублировали
    данные, зашифрованные другим);
  * метка версии обязана помещаться в поле БД (<=8 символов) и состоять
    только из безопасных символов;
  * активная версия обязана присутствовать в реестре.

Неявные значения по умолчанию (jwt из JWT_SECRET, v1 из PHONE_ENC_KEY)
явные источники могут ПЕРЕОПРЕДЕЛЯТЬ без конфликта — это и есть штатный
механизм ротации: после смены JWT прежнее значение кладут в keyring под
меткой jwt, и оно перекрывает выведенное из нового JWT_SECRET.

Сообщения об ошибках называют назначение, версию и источники, но НИКОГДА
не сами секреты.
"""
from __future__ import annotations

import re

# метка версии: латиница/цифры, до 8 символов (ровно столько в *_ver-полях)
VERSION_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


class KeyConfigError(RuntimeError):
    """Некорректная конфигурация ключей. Не старт с такой конфигурацией
    лучше, чем тихая потеря или дублирование зашифрованных данных."""


class Source:
    """Именованный источник версий. implicit=True — значение по умолчанию
    (из JWT_SECRET/PHONE_ENC_KEY), которое явные источники перекрывают без
    конфликта."""

    __slots__ = ("name", "implicit", "keys")

    def __init__(self, name: str, keys: dict[str, str], implicit: bool = False):
        self.name = name
        self.keys = keys
        self.implicit = implicit


def build_registry(purpose: str, sources: list[Source]) -> dict[str, str]:
    """{версия: секрет}. Бросает KeyConfigError при конфликте секретов
    одной версии или недопустимой метке."""
    reg: dict[str, tuple[str, str, bool]] = {}   # ver -> (secret, source, implicit)
    for src in sources:
        for ver, secret in src.keys.items():
            if not VERSION_RE.match(ver):
                raise KeyConfigError(
                    f"{purpose}: недопустимая метка версии {ver!r} "
                    f"(источник {src.name}); допустимо до 8 латинских "
                    "букв/цифр")
            if ver not in reg:
                reg[ver] = (secret, src.name, src.implicit)
                continue
            prev_secret, prev_name, prev_implicit = reg[ver]
            if prev_secret == secret:
                continue                       # тот же секрет — не конфликт
            if prev_implicit and not src.implicit:
                reg[ver] = (secret, src.name, src.implicit)   # ротация
            elif src.implicit and not prev_implicit:
                continue                       # явный уже победил
            else:
                raise KeyConfigError(
                    f"{purpose}: версия {ver!r} задана с разными секретами в "
                    f"источниках {prev_name} и {src.name}; версии "
                    "неизменяемы — исправьте конфигурацию")
    return {ver: secret for ver, (secret, _n, _i) in reg.items()}


def parse_keyring(raw: str | None) -> dict[str, str]:
    """{версия: секрет} из строки `ver:secret,ver:secret`."""
    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        ver, secret = chunk.split(":", 1)
        ver, secret = ver.strip(), secret.strip()
        if ver and secret:
            out[ver] = secret
    return out


def parse_versions(raw: str | None) -> list[str]:
    return [v.strip() for v in (raw or "").split(",") if v.strip()]
