"""
Надёжность доставки outbox: временный сбой отправки не должен терять
сообщение навсегда — только после MAX_OUTBOX_ATTEMPTS неудач подряд.

Изолированный in-memory движок (не общий SessionLocal из conftest), чтобы
не зависеть от порядка выполнения других тестов, использующих реальную
файловую БД напрямую через app.db.engine.SessionLocal.
"""
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.entities import Base
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService
from app.services import tasks


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _seed_outbox_message(maker) -> int:
    async with maker() as s:
        g = GlobalRepository(s)
        t = await g.create_tenant(name="Outbox-клуб")
        await s.commit()
        svc = BookingService(s, t.id)
        await svc.repo.enqueue("tg", 42, "тест-сообщение")
        await s.commit()
        return t.id


async def test_outbox_retries_transient_failure_then_gives_up(monkeypatch, maker):
    """Регресс: раньше сообщение помечалось sent=True при ЛЮБОЙ ошибке
    отправки, теряя его навсегда после первого же сбоя (даже временного)."""
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    await _seed_outbox_message(maker)

    calls = {"n": 0}

    async def failing_sender(user_id, text, tenant_id=None):
        calls["n"] += 1
        raise RuntimeError("временная ошибка сети")

    tasks.register_sender("tg", failing_sender)
    try:
        # попытки до лимита — сообщение остаётся в очереди для повтора
        for _ in range(tasks.MAX_OUTBOX_ATTEMPTS - 1):
            await tasks._deliver_once()
        assert calls["n"] == tasks.MAX_OUTBOX_ATTEMPTS - 1

        async with maker() as s:
            pending = await GlobalRepository(s).fetch_pending_outbox("tg")
            assert len(pending) == 1
            assert pending[0].attempts == tasks.MAX_OUTBOX_ATTEMPTS - 1

        # последняя попытка — лимит исчерпан, сообщение снимается с очереди
        await tasks._deliver_once()
        assert calls["n"] == tasks.MAX_OUTBOX_ATTEMPTS
        async with maker() as s:
            assert await GlobalRepository(s).fetch_pending_outbox("tg") == []
    finally:
        tasks._senders.pop("tg", None)


async def test_outbox_delivers_on_success_without_retry(monkeypatch, maker):
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    await _seed_outbox_message(maker)

    delivered = []

    async def ok_sender(user_id, text, tenant_id=None):
        delivered.append((user_id, text))

    tasks.register_sender("tg", ok_sender)
    try:
        await tasks._deliver_once()
        assert delivered == [(42, "тест-сообщение")]
        async with maker() as s:
            assert await GlobalRepository(s).fetch_pending_outbox("tg") == []
    finally:
        tasks._senders.pop("tg", None)
