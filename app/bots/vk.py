"""
VK-бот (vkbottle), мультитенантный. Импорт vkbottle ленивый.

Поддерживает два способа получения сообщений:
  • Long Poll  — бот сам опрашивает VK (run_polling). Обработчик регистрируется
    в vkbottle и вызывает общую логику _handle_message.
  • Callback API — VK шлёт события на /webhook/vk (feed_callback_event).

Оба пути ведут в одну функцию _handle_message.
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
    """Профиль VK (имя, screen_name, аватар) → subscribers; вернуть имя."""
    if not _bot:
        return f"vk{user_id}"
    try:
        profile = await fetch_vk_profile(_bot.api, user_id)
        await svc.repo.upsert_subscriber(
            PLATFORM, user_id, profile.name,
            username=profile.username, photo_url=profile.photo_url)
        return profile.name
    except Exception as e:
        logger.warning("Не удалось получить профиль VK %s: %s", user_id, e)
        return f"vk{user_id}"


async def _resolve_tenant(session, group_id):
    """
    Находит клуб по vk_group_id. При Long Poll group_id может не прийти —
    тогда берём единственный клуб с заданным vk_group_id.
    """
    g = GlobalRepository(session)
    tenants = await g.list_tenants()
    if group_id:
        for t in tenants:
            if t.vk_group_id == group_id:
                return t
    # запасной путь: первый клуб с непустым vk_group_id
    for t in tenants:
        if t.vk_group_id:
            return t
    return None


async def _handle_message(user_id: int, text: str, group_id=None) -> None:
    """Единая обработка входящего сообщения (общая для Long Poll и Callback)."""
    text = (text or "").strip().lower()
    async with SessionLocal() as session:
        tenant = await _resolve_tenant(session, group_id)
        if tenant is None:
            logger.warning("VK: клуб для group_id=%s не найден", group_id)
            return

        svc = BookingService(session, tenant.id, tz=tenant.timezone)
        name = await _upsert_vk_user(svc, user_id)
        await session.commit()

        if text in ("начать", "start", "список", "тренировки"):
            trainings = await svc.repo.list_upcoming()
            if not trainings:
                await _send(user_id, "Ближайших тренировок нет.")
                return
            for tr in trainings:
                await _send(
                    user_id,
                    f"🏸 {tr.title} ({svc.format_local(tr.start_at)})\n"
                    f"Записаться: напишите «записаться {tr.id}»")

        elif text.startswith("записаться "):
            try:
                tid = int(text.split()[1])
            except (IndexError, ValueError):
                await _send(user_id, "Формат: записаться <номер тренировки>")
                return
            res = await svc.sign_up(tid, PLATFORM, user_id, name)
            await session.commit()
            msg = {
                "active": "✅ Вы записаны!",
                "queue": f"⏳ Вы в очереди №{res.position}.",
                "already": "Вы уже записаны.",
                "closed": "Запись закрыта.",
            }.get(res.result, "Готово.")
            await _send(user_id, msg)

        elif text.startswith("отмена "):
            try:
                tid = int(text.split()[1])
            except (IndexError, ValueError):
                await _send(user_id, "Формат: отмена <номер тренировки>")
                return
            res = await svc.cancel_signup(tid, PLATFORM, user_id,
                                          lock_minutes=tenant.cancel_lock_minutes)
            await session.commit()
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

        else:
            await _send(
                user_id,
                "Команды:\n"
                "• список — ближайшие тренировки\n"
                "• записаться <номер>\n"
                "• отмена <номер>\n"
                "• профиль — ваша статистика")


async def setup() -> None:
    global _bot, _enabled
    if _enabled:
        return
    if not settings.vk_token:
        logger.warning("VK_TOKEN не задан — VK отключён.")
        return
    try:
        from vkbottle import Bot, BaseStateGroup  # noqa: F401
        from vkbottle.bot import Message
    except ImportError:
        logger.warning("vkbottle не установлен — VK отключён.")
        return

    _bot = Bot(token=settings.vk_token)
    _enabled = True

    # регистрируем обработчик всех сообщений для Long Poll
    @_bot.on.message()
    async def _on_message(message: Message):
        gid = getattr(message, "group_id", None)
        await _handle_message(message.from_id, message.text or "", gid)

    tasks.register_sender(PLATFORM, _send)
    logger.info("VK готов (Long Poll обработчик зарегистрирован).")


async def run_polling() -> None:
    if _bot:
        logger.info("VK: запускаю Long Poll…")
        await _bot.run_polling()


async def feed_callback_event(body: dict) -> None:
    """Обработка события VK Callback API (webhook)."""
    if not _enabled:
        return
    if body.get("type") != "message_new":
        return
    obj = body.get("object", {}).get("message", {})
    group_id = body.get("group_id")
    user_id = obj.get("from_id")
    text = obj.get("text") or ""
    await _handle_message(user_id, text, group_id)
