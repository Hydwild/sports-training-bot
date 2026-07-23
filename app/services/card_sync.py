"""
Единая точка обновления уже отправленных карточек занятия.

Данные синхронизировать не нужно: база одна, и сайт с обоими ботами читают
её напрямую. Расходиться могут только УЖЕ ОТПРАВЛЕННЫЕ сообщения — они
снимки и сами себя не перерисовывают.

Раньше каждый канал чинил это по-своему: запись через сайт дёргала
телеграмную карточку, VK дёргал телеграмную, а VK-карточку не обновлял
никто, кроме самого VK. Достаточно было записаться с сайта — и у человека
в VK-переписке навсегда оставалось старое число мест.

Здесь один вызов, который обновляет ВСЕ каналы. Любое изменение занятия
(запись, отмена, гость, перенос, смена лимита) должно звать именно его —
тогда новый канал достаточно добавить в одном месте.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("app")


async def notify_slot_changed(tenant_id: int, training_id: int) -> None:
    """Перерисовывает карточки занятия во всех каналах.

    Ошибки каналов не пробрасываем: обновление карточки — украшение поверх
    уже совершённого действия. Запись состоялась и сохранена; если Telegram
    недоступен, это не повод возвращать посетителю ошибку."""
    for channel, refresh in (("telegram", _refresh_telegram),
                             ("vk", _refresh_vk)):
        try:
            await refresh(tenant_id, training_id)
        except Exception as e:              # noqa: BLE001
            logger.warning("Карточка (%s) клуба %s не обновлена: %s",
                           channel, tenant_id, type(e).__name__)


async def _refresh_telegram(tenant_id: int, training_id: int) -> None:
    from app.bots import telegram as tg
    await tg._refresh_group_card(tenant_id, training_id)


async def _refresh_vk(tenant_id: int, training_id: int) -> None:
    from app.bots import vk
    await vk.refresh_cards(tenant_id, training_id)
