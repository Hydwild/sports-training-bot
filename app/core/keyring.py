"""
Строгий реестр версионированных ключей — общий для телефонов и токенов ботов.

Версии объявлены НЕИЗМЕНЯЕМЫМИ: под конкретной меткой (`jwt`, `v1`, `v2`)
зашифрованы конкретные строки в базе. Поэтому:

  * одна версия с ОДНИМ секретом в нескольких источниках — допустимо;
  * одна версия с РАЗНЫМИ секретами — ошибка конфигурации. Это касается и
    дублей ВНУТРИ одной переменной (`PHONE_KEYS=v1:A,v1:B`), и конфликта
    выделенного ключа с реестром (`PHONE_ENC_KEY`/`BOT_TOKEN_ENC_KEY` против
    `*_KEYS`): мы бы молча взяли один секрет и потеряли/задублировали
    данные, зашифрованные другим;
  * метка версии обязана помещаться в поле БД (<=8 символов) и состоять
    только из безопасных символов;
  * активная версия обязана присутствовать в реестре.

Единственное сознательно разрешённое перекрытие «одна версия — разные
секреты» — историческая метка `jwt`. Ключ этой версии по умолчанию выведен
из текущего JWT_SECRET; после ротации JWT прежнее значение кладут в keyring
под меткой `jwt`, и оно ОБЯЗАНО перекрыть выведенное из нового секрета,
иначе старые телефоны станут нечитаемы. Это перекрытие включается флагом
`overridable=True` ровно на источнике JWT_SECRET→jwt — и ни на каком другом.

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
    """Именованный источник пар (версия, секрет).

    overridable=True — исторический дефолт (jwt, выведенный из JWT_SECRET),
    который явный источник может СОЗНАТЕЛЬНО перекрыть: это единственный
    легитимный случай «одна версия — разные секреты», нужный для ротации
    JWT_SECRET без потери старых телефонов. Для всех прочих версий разные
    секреты одной версии — ошибка, включая дубли внутри одной переменной и
    конфликт PHONE_ENC_KEY/BOT_TOKEN_ENC_KEY с *_KEYS.

    keys — dict[str, str] ИЛИ list[tuple[str, str]]. Список сохраняет дубли
    (несколько записей одной версии в одной переменной), и именно так они
    доходят до проверки конфликтов, а не схлопываются молча."""

    __slots__ = ("name", "pairs", "overridable")

    def __init__(self, name: str,
                 keys: dict[str, str] | list[tuple[str, str]],
                 overridable: bool = False):
        self.name = name
        self.pairs: list[tuple[str, str]] = (
            list(keys.items()) if isinstance(keys, dict) else list(keys))
        self.overridable = overridable


def build_registry(purpose: str, sources: list[Source]) -> dict[str, str]:
    """{версия: секрет}. Бросает KeyConfigError при конфликте секретов
    одной версии или недопустимой метке."""
    reg: dict[str, tuple[str, str, bool]] = {}   # ver -> (secret, source, overridable)
    for src in sources:
        for ver, secret in src.pairs:
            if not VERSION_RE.match(ver):
                raise KeyConfigError(
                    f"{purpose}: недопустимая метка версии {ver!r} "
                    f"(источник {src.name}); допустимо до 8 латинских "
                    "букв/цифр")
            if ver not in reg:
                reg[ver] = (secret, src.name, src.overridable)
                continue
            prev_secret, prev_name, prev_overridable = reg[ver]
            if prev_secret == secret:
                continue                       # тот же секрет — не конфликт
            # разные секреты одной НЕИЗМЕНЯЕМОЙ версии
            if prev_overridable and not src.overridable:
                reg[ver] = (secret, src.name, src.overridable)   # обоснованное перекрытие jwt
            elif src.overridable and not prev_overridable:
                continue                       # явный уже победил исторический дефолт
            else:
                raise KeyConfigError(
                    f"{purpose}: версия {ver!r} задана с разными секретами в "
                    f"источниках {prev_name} и {src.name}; версии неизменяемы "
                    "— исправьте конфигурацию (перекрытие допустимо только для "
                    "исторической версии jwt при ротации JWT_SECRET)")
    return {ver: secret for ver, (secret, _n, _o) in reg.items()}


def parse_keyring_pairs(raw: str | None) -> list[tuple[str, str]]:
    """[(версия, секрет)] из строки `ver:secret,ver:secret` — С сохранением
    дублей, чтобы `v1:A,v1:B` дошло до проверки конфликтов, а не схлопнулось."""
    out: list[tuple[str, str]] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        ver, secret = chunk.split(":", 1)
        ver, secret = ver.strip(), secret.strip()
        if ver and secret:
            out.append((ver, secret))
    return out


def parse_keyring(raw: str | None) -> dict[str, str]:
    """{версия: секрет} из строки `ver:secret,ver:secret`. Схлопывает дубли —
    используйте parse_keyring_pairs, если дубли нужно проверить на конфликт."""
    return dict(parse_keyring_pairs(raw))


def parse_versions(raw: str | None) -> list[str]:
    return [v.strip() for v in (raw or "").split(",") if v.strip()]
