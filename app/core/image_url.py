"""
Проверка адресов внешних изображений (фото мастеров, обложка клуба).

Адрес вставляется в <img src> публичной страницы: браузер посетителя сам
пойдёт к этому хосту. Отсюда две опасности, которые здесь и закрываются:

  * схема: только https. Не javascript:/data:/file: (XSS, локальные
    файлы) и не plain http (смешанный контент, перехват).
  * хост: не loopback, не приватные и не link-local адреса, не
    метадата-эндпоинт облака. Даже без серверного проксирования такой
    адрес в <img> — это запрос ИЗ браузера оператора/посетителя во
    внутреннюю сеть; а если позже появится image-proxy, наивная проверка
    открыла бы SSRF.

Полную защиту от SSRF (повторная проверка после DNS и редиректов) даёт
только image-proxy — его здесь нет намеренно: наивный proxy хуже, чем
его отсутствие. См. комментарий в задании (блок 11).
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

MAX_URL_LEN = 500

# хосты, которые нельзя резолвить наружу пользовательским вводом
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain",
                      "metadata", "metadata.google.internal"}


def _host_is_forbidden(host: str) -> bool:
    host = host.strip("[]").lower()      # [::1] -> ::1
    if not host or host in _BLOCKED_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # это имя, а не литеральный адрес. Резолвить его здесь не будем
        # (это работа image-proxy); литеральные приватные адреса ловим ниже
        return False
    # 127.0.0.0/8, ::1, 10/8, 172.16/12, 192.168/16, 169.254/16 (в т.ч.
    # облачный метадата-эндпоинт 169.254.169.254), fc00::/7, ::, и пр.
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate_image_url(value: str | None) -> str | None:
    """Нормализованный https-URL или None (пусто). Бросает ValueError с
    человеко-понятной причиной при недопустимом адресе."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if len(v) > MAX_URL_LEN:
        raise ValueError("ссылка слишком длинная")

    parsed = urlparse(v)
    if parsed.scheme != "https":
        # именно https: plain http перехватывается и ломает mixed-content
        raise ValueError("ссылка на картинку должна начинаться с https://")
    if not parsed.hostname:
        raise ValueError("в ссылке нет адреса хоста")
    if _host_is_forbidden(parsed.hostname):
        raise ValueError("этот адрес недопустим (локальный или внутренний)")
    return v
