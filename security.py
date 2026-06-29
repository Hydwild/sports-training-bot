"""
Получение информации о пользователе из Telegram и ВКонтакте.

Telegram: username из апдейта (есть сразу), аватар — через getUserProfilePhotos
          (запрашивается фоново, кэшируется в БД).

VK: имя, screen_name и аватар — через users.get с одним запросом при первом
    взаимодействии (кэшируется в БД).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("user_info")


@dataclass
class UserProfile:
    name: str
    username: str | None       # @nickname (без @)
    photo_url: str | None      # URL аватара


# ---------- Telegram ----------

async def fetch_tg_photo_url(bot, user_id: int) -> str | None:
    """
    Запрашивает последний аватар Telegram-пользователя.
    Возвращает URL или None при любой ошибке.
    """
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos.photos:
            return None
        file_id = photos.photos[0][-1].file_id
        file = await bot.get_file(file_id)
        return f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
    except Exception as e:
        logger.debug("TG аватар user_id=%s: %s", user_id, e)
        return None


# ---------- ВКонтакте ----------

async def fetch_vk_profile(api, user_id: int) -> UserProfile:
    """
    Запрашивает профиль VK-пользователя: полное имя, screen_name, аватар 200px.
    Возвращает UserProfile; при ошибке — заглушку с id.
    """
    try:
        users = await api.users.get(
            user_ids=[user_id],
            fields=["screen_name", "photo_200"],
        )
        if not users:
            raise ValueError("empty response")
        u = users[0]
        name = f"{u.first_name} {u.last_name}".strip()
        # screen_name бывает вида "id123456" если не задан пользовательский
        screen = u.screen_name or None
        if screen and screen.startswith("id") and screen[2:].isdigit():
            screen = None  # не показываем технический screen_name
        photo = getattr(u, "photo_200", None)
        return UserProfile(name=name or f"vk{user_id}",
                           username=screen,
                           photo_url=photo)
    except Exception as e:
        logger.debug("VK профиль user_id=%s: %s", user_id, e)
        return UserProfile(name=f"vk{user_id}", username=None, photo_url=None)


# ---------- Общее ----------

def profile_link(username: str | None, user_id: int | None = None,
                 platform: str = "tg") -> str | None:
    """Ссылка на профиль пользователя."""
    if platform == "vk":
        if username:
            return f"https://vk.com/{username}"
        if user_id:
            return f"https://vk.com/id{user_id}"
        return None
    # Telegram
    if username:
        return f"https://t.me/{username}"
    if user_id:
        return f"tg://user?id={user_id}"
    return None
