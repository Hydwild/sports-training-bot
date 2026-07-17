"""
Безопасность: JWT-токены, проверка подписи Telegram Login Widget, роли.

Вход в админку — через Telegram (без паролей). Telegram Login Widget
возвращает данные пользователя с полем hash, подписанным секретом бота.
Мы проверяем подпись, находим роли пользователя в клубах и выдаём JWT.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import time

import jwt
from fastapi import Depends, HTTPException, Request

from app.core.config import settings

ALGO = "HS256"
# Иерархия ролей: owner > coach > assistant
ROLE_LEVEL = {"assistant": 1, "coach": 2, "owner": 3}


# ---------- JWT ----------

def create_token(tg_user_id: int, tenant_id: int, role: str,
                 name: str = "", ttl_hours: int = 12) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(tg_user_id),
        "tenant_id": tenant_id,
        "role": role,
        "name": name,
        "iat": now,
        "exp": now + dt.timedelta(hours=ttl_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="Токен истёк") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="Неверный токен") from e


# ---------- Telegram Login Widget ----------

def verify_telegram_auth(data: dict) -> bool:
    """
    Проверка подписи данных Telegram Login Widget.
    secret_key = SHA256(bot_token); проверяем HMAC-SHA256 над data_check_string.
    Также проверяем свежесть auth_date (не старше 24 ч).
    """
    if not settings.tg_token:
        return False
    received_hash = data.get("hash")
    if not received_hash:
        return False
    auth_date = int(data.get("auth_date", 0))
    if time.time() - auth_date > 86400:
        return False  # данные устарели

    check = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={check[k]}" for k in sorted(check))
    secret_key = hashlib.sha256(settings.tg_token.encode()).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(),
                         hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc_hash, received_hash)


# ---------- Зависимости FastAPI: текущий пользователь и роли ----------

def _read_token(request: Request) -> str:
    # сначала cookie (для HTML-админки), затем заголовок Bearer (для API)
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return token


async def current_claims(request: Request) -> dict:
    return decode_token(_read_token(request))


def require_role(min_role: str):
    """Зависимость: требует роль не ниже указанной."""
    async def checker(claims: dict = Depends(current_claims)) -> dict:
        have = ROLE_LEVEL.get(claims.get("role", ""), 0)
        need = ROLE_LEVEL[min_role]
        if have < need:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return claims
    return checker


# ---------- CSRF (для форм HTML-админки) ----------

def csrf_token_for(session_value: str) -> str:
    """CSRF-токен, привязанный к значению сессионной cookie. Без знания
    jwt_secret подделать нельзя, а сторонний сайт не может прочитать
    httponly-cookie — поэтому и вычислить корректный токен для формы не
    сможет. Годится для любой сессионной cookie (JWT тенант-админки или
    сырой платформенный токен), значение которой достаточно секретно."""
    return hmac.new(settings.jwt_secret.encode(),
                    b"csrf:" + session_value.encode(),
                    hashlib.sha256).hexdigest()


def csrf_for_request(request: Request, cookie_name: str = "access_token") -> str:
    """CSRF-токен для встраивания в форму (пустая строка, если нет сессии)."""
    tok = request.cookies.get(cookie_name, "")
    return csrf_token_for(tok) if tok else ""


def require_csrf(cookie_name: str = "access_token"):
    """Зависимость для POST-роутов админки: сверяет поле формы `csrf` против
    указанной сессионной cookie (по умолчанию — access_token тенант-админки)."""
    async def checker(request: Request) -> None:
        tok = request.cookies.get(cookie_name, "")
        form = await request.form()  # Starlette кэширует — повторный await безопасен
        sent = str(form.get("csrf", ""))
        if not tok or not hmac.compare_digest(sent, csrf_token_for(tok)):
            raise HTTPException(status_code=403, detail="CSRF-проверка не пройдена")
    return checker
