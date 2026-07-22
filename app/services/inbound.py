"""Durable inbox для Telegram/VK webhook-событий."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.engine import SessionLocal
from app.models.entities import InboundEvent

logger = logging.getLogger("inbound")

MAX_ATTEMPTS = 5
STALE_SECONDS = 300
DONE_RETENTION_HOURS = 24
_wakeup = asyncio.Event()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class ClaimedEvent:
    id: int
    platform: str
    tenant_id: int
    payload: dict[str, Any]


async def ingest(
    *, platform: str, tenant_id: int, external_event_id: str,
    payload: dict[str, Any],
) -> bool:
    """Фиксирует update до HTTP-ответа. False означает безопасный duplicate."""
    row = InboundEvent(
        platform=platform,
        tenant_id=tenant_id,
        external_event_id=external_event_id,
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    async with SessionLocal() as session:
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
    _wakeup.set()  # только после успешного commit
    return True


async def _claim_one() -> ClaimedEvent | None:
    now = _now()
    async with SessionLocal() as session:
        stmt = (
            select(InboundEvent)
            .where(
                InboundEvent.status == "pending",
                or_(InboundEvent.next_attempt_at.is_(None),
                    InboundEvent.next_attempt_at <= now),
            )
            .order_by(InboundEvent.id)
            .limit(1)
        )
        # PostgreSQL: несколько реплик не забирают один update. SQLite
        # игнорирует смысл SKIP LOCKED, но в dev работает один процесс.
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        async with session.begin():
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            row.status = "processing"
            row.claimed_at = now
            event = ClaimedEvent(
                id=row.id,
                platform=row.platform,
                tenant_id=row.tenant_id,
                payload=json.loads(row.payload),
            )
        return event


async def _process(event: ClaimedEvent) -> None:
    if event.platform == "tg":
        from app.bots import telegram
        await telegram.feed_webhook_update(event.payload, tenant_id=event.tenant_id)
    elif event.platform == "vk":
        from app.bots import vk
        await vk.feed_callback_event(event.payload)
    else:
        raise ValueError(f"неизвестная inbound platform: {event.platform}")


async def _mark_done(event_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(InboundEvent).where(InboundEvent.id == event_id).values(
                status="done", payload="{}", processed_at=_now(),
                claimed_at=None, last_error="",
            )
        )
        await session.commit()


async def _mark_failed(event_id: int, exc: Exception) -> None:
    async with SessionLocal() as session:
        row = await session.get(InboundEvent, event_id)
        if row is None:
            return
        attempts = (row.attempts or 0) + 1
        row.attempts = attempts
        row.claimed_at = None
        row.last_error = f"{type(exc).__name__}: {exc}"[:500]
        if attempts >= MAX_ATTEMPTS:
            row.status = "dead"
            row.payload = "{}"
            row.processed_at = _now()
            logger.error("Inbound id=%s platform=%s помещён в dead-letter",
                         row.id, row.platform)
        else:
            delays = (5, 30, 120, 600)
            row.status = "pending"
            row.next_attempt_at = _now() + dt.timedelta(
                seconds=delays[min(attempts - 1, len(delays) - 1)]
            )
        await session.commit()


async def _maintenance() -> None:
    now = _now()
    stale = now - dt.timedelta(seconds=STALE_SECONDS)
    cutoff = now - dt.timedelta(hours=DONE_RETENTION_HOURS)
    async with SessionLocal() as session:
        result = await session.execute(
            update(InboundEvent)
            .where(InboundEvent.status == "processing",
                   InboundEvent.claimed_at < stale)
            .values(status="pending", claimed_at=None)
        )
        await session.execute(
            delete(InboundEvent).where(
                InboundEvent.status.in_(("done", "dead")),
                InboundEvent.processed_at < cutoff,
            )
        )
        await session.commit()
        if result.rowcount:
            logger.warning("Возвращено зависших inbound-событий: %d", result.rowcount)


async def worker_loop() -> None:
    last_maintenance = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    while True:
        try:
            # Очищаем ДО запроса: commit между SELECT и wait оставит Event
            # установленным и не потеряется в окне гонки.
            _wakeup.clear()
            event = await _claim_one()
            if event is not None:
                try:
                    await _process(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Ошибка inbound id=%s", event.id)
                    await _mark_failed(event.id, exc)
                else:
                    await _mark_done(event.id)
                continue

            now = _now()
            if (now - last_maintenance).total_seconds() >= 60:
                await _maintenance()
                last_maintenance = now
            try:
                await asyncio.wait_for(_wakeup.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Inbound worker упал; повтор через 2 секунды")
            await asyncio.sleep(2)
