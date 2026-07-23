"""
Публичный адрес страницы записи клуба.

По умолчанию это наша страница `/club/<id>`, но у клиента может быть свой
сайт или домен — тогда в кнопку бота, в QR-код и в панель должна уходить
ЕГО ссылка, иначе напечатанный QR ведёт не туда, куда клиент рассчитывает.

Адрес попадает в inline-кнопку Telegram и в QR-код, поэтому принимается
только https и только внешний хост: `javascript:` в кнопке — это XSS у
всех, кто её нажмёт, а внутренний адрес в QR — попытка увести посетителя
во внутреннюю сеть. Проверку переиспользуем из image_url: там уже описаны
и схема, и запрещённые хосты.
"""
from __future__ import annotations

import re

from app.core.config import settings
from app.core.image_url import MAX_URL_LEN, _host_is_forbidden

# Короткий адрес: строчные латинские буквы, цифры и дефис, 3–40 символов,
# без дефиса по краям. Читается вслух и печатается в QR.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])?$")

# Адреса, которые нельзя занимать: они уже что-то значат в наших ссылках
# либо выглядят как служебные.
RESERVED_SLUGS = {"club", "admin", "api", "health", "promo", "faq", "reviews",
                  "static", "webhook", "c", "m", "qr", "login", "logout"}


def validate_slug(value: str | None) -> str | None:
    """Короткий адрес клуба или None. Бросает ValueError с текстом для
    оператора — он показывается прямо в форме."""
    v = (value or "").strip().lower().lstrip("/")
    if not v:
        return None
    if v.isdigit():
        # иначе /c/3 и /club/3 читались бы как одно и то же и путали бы
        raise ValueError("адрес не может состоять только из цифр")
    if not SLUG_RE.match(v):
        raise ValueError(
            "допустимы строчные латинские буквы, цифры и дефис, "
            "от 3 до 40 символов (например salon-hortensia)")
    if v in RESERVED_SLUGS:
        raise ValueError(f"адрес «{v}» зарезервирован, выберите другой")
    return v


# @username бота: по правилам Telegram — латиница, цифры и подчёркивание,
# 5–32 символа. Это НЕ секрет (в отличие от токена), нужен только чтобы
# построить ссылку t.me/<username>.
BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def validate_bot_username(value: str | None) -> str | None:
    v = (value or "").strip().lstrip("@")
    if not v:
        return None
    if v.startswith("https://t.me/") or v.startswith("t.me/"):
        v = v.rsplit("/", 1)[-1]
    if not BOT_USERNAME_RE.match(v):
        raise ValueError("имя бота — латиница, цифры и подчёркивание, "
                         "5–32 символа (например MyClubBot)")
    return v


def bot_link(tenant) -> str | None:
    """Ссылка на бота клуба в Telegram или None, если username не задан."""
    name = (getattr(tenant, "bot_username", "") or "").strip().lstrip("@")
    return f"https://t.me/{name}" if name else None


def validate_site_url(value: str | None) -> str | None:
    """Свой адрес клиента или None. Бросает ValueError с человеческим
    текстом — он показывается оператору прямо в форме."""
    from urllib.parse import urlparse

    v = (value or "").strip()
    if not v:
        return None
    if len(v) > MAX_URL_LEN:
        raise ValueError("ссылка слишком длинная")
    parsed = urlparse(v)
    if parsed.scheme != "https":
        raise ValueError("ссылка на сайт должна начинаться с https://")
    if not parsed.hostname:
        raise ValueError("в ссылке нет адреса хоста")
    if _host_is_forbidden(parsed.hostname):
        raise ValueError("этот адрес недопустим (локальный или внутренний)")
    return v


def club_site_url(tenant) -> str:
    """Адрес страницы записи клуба: свой, если задан, иначе наш /club/<id>.

    Бросает RuntimeError, если своего адреса нет и PUBLIC_BASE_URL не
    настроен — построить абсолютную ссылку тогда не из чего."""
    custom = (getattr(tenant, "site_url", "") or "").strip()
    if custom:
        return custom
    return settings.public_url(club_path(tenant))


def club_path(tenant) -> str:
    """Путь страницы записи на НАШЕМ домене: короткий, если задан адрес.

    `/club/<id>` остаётся рабочим всегда — по нему уже сделаны ссылки и
    QR-коды, ломать их нельзя."""
    slug = (getattr(tenant, "slug", "") or "").strip()
    return f"/c/{slug}" if slug else f"/club/{tenant.id}"


def club_site_url_or_none(tenant) -> str | None:
    """То же, но без исключения: None, когда ссылку построить не из чего.
    Удобно там, где отсутствие адреса — не ошибка, а просто «не показываем»."""
    try:
        return club_site_url(tenant)
    except RuntimeError:
        return None
