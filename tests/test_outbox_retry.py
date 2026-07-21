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


async def _make_due(maker):
    """Сдвигает время повтора в прошлое — как будто пауза уже прошла.
    После неудачи сообщение ждёт (1, 2, 5, 15 минут), иначе заблокированный
    бот перебирался бы на каждом проходе очереди."""
    from sqlalchemy import update

    from app.models.entities import Outbox
    async with maker() as s:
        await s.execute(update(Outbox).values(next_attempt_at=None))
        await s.commit()


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
            await _make_due(maker)
            await tasks._deliver_once()
        assert calls["n"] == tasks.MAX_OUTBOX_ATTEMPTS - 1

        async with maker() as s:
            pending = await GlobalRepository(s).fetch_pending_outbox("tg")
            assert len(pending) == 1
            assert pending[0].attempts == tasks.MAX_OUTBOX_ATTEMPTS - 1

        # последняя попытка — лимит исчерпан, сообщение снимается с очереди
        await _make_due(maker)
        await tasks._deliver_once()
        assert calls["n"] == tasks.MAX_OUTBOX_ATTEMPTS
        async with maker() as s:
            assert await GlobalRepository(s).fetch_pending_outbox("tg") == []
    finally:
        tasks._senders.pop("tg", None)


async def test_claim_pending_outbox_is_atomic_no_double_claim(maker):
    """Регресс: раньше выборка неотправленных сообщений была простым SELECT
    без блокировки — если бы приложение запустили в двух экземплярах
    одновременно, оба забрали бы одну и ту же запись и оба бы её отправили.
    claim_pending_outbox — атомарный UPDATE ... WHERE sent=False ...
    RETURNING: вторая "параллельная" попытка захвата той же записи должна
    вернуть пустой список."""
    await _seed_outbox_message(maker)

    async with maker() as s1:
        g1 = GlobalRepository(s1)
        first = await g1.claim_pending_outbox("tg")
        await s1.commit()
        assert len(first) == 1

    async with maker() as s2:
        g2 = GlobalRepository(s2)
        second = await g2.claim_pending_outbox("tg")
        assert second == []  # уже захвачено первым "экземпляром"


async def test_undelivered_message_becomes_dead_not_silently_sent(
        monkeypatch, maker):
    """Регресс: исчерпав попытки, сообщение помечалось sent=True — провал
    доставки становился неотличим от успеха, и о нём никто не узнавал.
    Теперь у него отдельное состояние dead и сохранённая причина."""
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    await _seed_outbox_message(maker)

    async def failing_sender(user_id, text, tenant_id=None):
        raise RuntimeError("бот заблокирован пользователем")

    tasks.register_sender("tg", failing_sender)
    try:
        for _ in range(tasks.MAX_OUTBOX_ATTEMPTS):
            await _make_due(maker)
            await tasks._deliver_once()

        async with maker() as s:
            g = GlobalRepository(s)
            assert await g.fetch_pending_outbox("tg") == []
            assert await g.count_dead_outbox() == 1
            from sqlalchemy import select

            from app.models.entities import Outbox
            row = (await s.execute(select(Outbox))).scalar_one()
            assert row.status == "dead"
            assert "заблокирован" in row.last_error
    finally:
        tasks._senders.pop("tg", None)


async def test_claim_stuck_in_processing_is_requeued(monkeypatch, maker):
    """Ключевой сценарий: процесс убили (деплой, перезапуск) между захватом
    сообщения и его отправкой. Раньше запись навсегда оставалась помеченной
    как отправленная, и уведомление пропадало молча."""
    import datetime as dt

    from sqlalchemy import select

    from app.models.entities import Outbox

    monkeypatch.setattr(tasks, "SessionLocal", maker)
    await _seed_outbox_message(maker)

    # захват произошёл, отправка не началась — процесс прервали
    async with maker() as s:
        claimed = await GlobalRepository(s).claim_pending_outbox("tg")
        assert len(claimed) == 1
        await s.commit()

    async with maker() as s:
        row = (await s.execute(select(Outbox))).scalar_one()
        assert row.status == "processing"
        # состариваем захват, как будто прошло больше времени простоя
        row.claimed_at = (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(minutes=tasks.STALE_CLAIM_MINUTES + 1))
        await s.commit()

    delivered = []

    async def ok_sender(user_id, text, tenant_id=None):
        delivered.append(text)

    tasks.register_sender("tg", ok_sender)
    try:
        await tasks._deliver_once()
        assert delivered == ["тест-сообщение"], "сообщение потеряно"
        async with maker() as s:
            row = (await s.execute(select(Outbox))).scalar_one()
            assert row.status == "sent"
    finally:
        tasks._senders.pop("tg", None)


async def test_fresh_claim_is_not_stolen_by_requeue(maker):
    """Свежий захват трогать нельзя: иначе два прохода очереди отправят
    одно и то же сообщение дважды."""
    async with maker() as s:
        await GlobalRepository(s).claim_pending_outbox("tg")
        await s.commit()
    await _seed_outbox_message(maker)

    async with maker() as s:
        g = GlobalRepository(s)
        claimed = await g.claim_pending_outbox("tg")
        await s.commit()
        assert len(claimed) == 1
        assert await g.requeue_stale_outbox(10) == 0


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


async def test_failed_message_waits_before_retry(monkeypatch, maker):
    """Регресс: неудачная доставка повторялась на каждом проходе очереди —
    пять попыток сгорали за минуту, и живое сообщение попадало в dead
    из-за короткого сбоя сети."""
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    await _seed_outbox_message(maker)

    calls = {"n": 0}

    async def failing_sender(user_id, text, tenant_id=None):
        calls["n"] += 1
        raise RuntimeError("сеть недоступна")

    tasks.register_sender("tg", failing_sender)
    try:
        await tasks._deliver_once()
        assert calls["n"] == 1
        # сразу следующий проход сообщение не трогает — пауза не прошла
        await tasks._deliver_once()
        await tasks._deliver_once()
        assert calls["n"] == 1, "повтор без паузы"

        async with maker() as s:
            pending = await GlobalRepository(s).fetch_pending_outbox("tg")
            assert pending and pending[0].attempts == 1
            assert pending[0].next_attempt_at is not None

        # когда пауза прошла — повторяем
        await _make_due(maker)
        await tasks._deliver_once()
        assert calls["n"] == 2
    finally:
        tasks._senders.pop("tg", None)
