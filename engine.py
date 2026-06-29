"""
VK-бот (vkbottle), мультитенантный. Импорт vkbottle ленивый.

При каждом взаимодействии получаем полное имя, screen_name и аватар
пользователя через users.get и сохраняем в БД.
"""
from __future__ import annotations

import logging

from app.bots.user_info import fetch_vk_profile
from app.bots.views import profile_card
from app.core.config import settings
from app.db.engine import SessionLocal
from app.repositories.repo import GlobalRepository
from app.services import tasks
from app.services.booking import BookingService

logger = logging.getLogger("vk")
PLATFORM = "vk"

_bot = None   # type: ignore
_enabled = False


async def _send(user_id: int, text: str) -> None:
    if _bot:
        await _bot.api.messages.send(user_id=user_id, message=text, random_id=0)


async def _upsert_vk_user(svc: BookingService, user_id: int) -> str:
    """
    Запрашивает профиль ВКонтакте (имя, screen_name, аватар 200px),
    сохраняет в subscribers и возвращает отображаемое имя.
    В отличие от Telegram здесь аватар приходит в том же запросе — без фона.
    """
    if not _bot:
        return f"vk{user_id}"
    profile = await fetch_vk_profile(_bot.api, user_id)
    await svc.repo.upsert_subscriber(
        PLATFORM, user_id, profile.name,
        username=profile.username,
        photo_url=profile.photo_url,
    )
    return profile.name


async def setup() -> None:
    global _bot, _enabled
    if _enabled:
        return
    if not settings.vk_token:
        logger.warning("VK_TOKEN не задан — VK отключён.")
        return
    try:
        from vkbottle import Bot
    except ImportError:
        logger.warning("vkbottle не установлен — VK отключён.")
        return
    _bot = Bot(token=settings.vk_token)
    _enabled = True
    tasks.register_sender(PLATFORM, _send)
    logger.info("VK готов.")


async def run_polling() -> None:
    if _bot:
        await _bot.run_polling()


async def feed_callback_event(body: dict) -> None:
    """Обработка события VK Callback API."""
    if not _enabled:
        return
    if body.get("type") != "message_new":
        return
    obj = body.get("object", {}).get("message", {})
    group_id = body.get("group_id")
    user_id = obj.get("from_id")
    text = (obj.get("text") or "").strip().lower()

    async with SessionLocal() as session:
        g = GlobalRepository(session)
        tenant = None
        for t in await g.list_tenants():
            if t.vk_group_id == group_id:
                tenant = t
                break
        if tenant is None:
            return

        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        # получаем и сохраняем полный профиль при каждом взаимодействии
        name = await _upsert_vk_user(svc, user_id)
        await session.commit()

        if text in ("начать", "start", "список"):
            trainings = await svc.repo.list_upcoming()
            if not trainings:
                await _send(user_id, "Ближайших тренировок нет.")
                return
            for tr in trainings:
                await _send(user_id,
                            f"🏸 {tr.title} ({svc.format_local(tr.start_at)})"
                            f" — записаться: «записаться {tr.id}»")

        elif text.startswith("записаться "):
            try:
                tid = int(text.split()[1])
            except (IndexError, ValueError):
                return
            res = await svc.sign_up(tid, PLATFORM, user_id, name)
            msg = {
                "active": "✅ Вы записаны!",
                "queue": f"⏳ Очередь №{res.position}.",
                "already": "Вы уже записаны.",
                "closed": "Запись закрыта.",
            }[res.result]
            await _send(user_id, msg)

        elif text.startswith("отмена "):
            try:
                tid = int(text.split()[1])
            except (IndexError, ValueError):
                return
            res = await svc.cancel_signup(tid, PLATFORM, user_id,
                                          lock_minutes=tenant.cancel_lock_minutes)
            if res.get("locked"):
                await _send(user_id,
                            f"Отмена закрыта: до тренировки меньше "
                            f"{res['lock_minutes']} мин.")
            else:
                await _send(user_id,
                            "Запись отменена." if res["cancelled"]
                            else "Вы не были записаны.")

        elif text in ("профиль", "моя статистика"):
            stats = await svc.user_stats(PLATFORM, user_id)
            await _send(user_id, profile_card(name, stats))

        elif text in ("стоп", "отписаться"):
            await svc.repo.set_subscription(PLATFORM, user_id, False)
            await session.commit()
            await _send(user_id, "Вы отписались от рассылки.")
