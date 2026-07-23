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

from app.core.config import settings
from app.core.image_url import MAX_URL_LEN, _host_is_forbidden


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
    return settings.public_url(f"/club/{tenant.id}")


def club_site_url_or_none(tenant) -> str | None:
    """То же, но без исключения: None, когда ссылку построить не из чего.
    Удобно там, где отсутствие адреса — не ошибка, а просто «не показываем»."""
    try:
        return club_site_url(tenant)
    except RuntimeError:
        return None
