"""
Сообщения платформ без канала доставки не должны висеть в очереди вечно.

Боевой дефект: sender регистрируется только для настроенных площадок — VK
без VK_TOKEN его не получает вовсе. `_deliver_once` перебирает лишь
зарегистрированные senders, поэтому строки такой платформы НИКОГДА не
захватываются: попытки не растут, до dead они не доходят и висят в pending
вечно. Обычные tg-сообщения так застрять не могут — после
MAX_OUTBOX_ATTEMPTS они уходят в dead.

В проде это и случилось: самое старое сообщение ждало отправки 22 дня, а
алерт «очередь не разгребается» горел постоянно и перекрывал собой
настоящие сбои доставки.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.entities import Base, Outbox, Tenant
from app.repositories.repo import GlobalRepository, TenantRepository


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _seed(maker) -> int:
    """tg и vk ставятся штатным путём; legacy — платформа, которой в коде
    уже нет, но строки от неё в базе остались."""
    async with maker() as s:
        t = Tenant(name="Клуб")
        s.add(t)
        await s.commit()
        repo = TenantRepository(s, t.id)
        await repo.enqueue("tg", 111, "телеграм-напоминание")
        await repo.enqueue("vk", 333, "вк-напоминание")
        s.add(Outbox(tenant_id=t.id, platform="legacy", user_id=444,
                     text="от давно убранной площадки", sent=False))
        await s.commit()
        return t.id


async def _statuses(maker) -> dict[str, str]:
    async with maker() as s:
        rows = (await s.execute(select(Outbox.platform, Outbox.status))).all()
        return {p: st for p, st in rows}


async def test_undeliverable_platform_is_buried(maker):
    """Подключён только Telegram: vk и legacy хоронятся с причиной, tg
    остаётся в очереди нетронутым."""
    await _seed(maker)
    async with maker() as s:
        buried = await GlobalRepository(s).dead_letter_undeliverable(
            ["tg"], "нет канала доставки для этой платформы")
        await s.commit()
    assert buried == 2
    st = await _statuses(maker)
    assert st["vk"] == "dead" and st["legacy"] == "dead"
    assert st["tg"] == "pending"

    async with maker() as s:
        row = (await s.execute(select(Outbox).where(
            Outbox.platform == "vk"))).scalar_one()
        assert "нет канала доставки" in (row.last_error or "")


async def test_nothing_buried_before_bots_are_up(maker):
    """Пустой список подключённых платформ = боты ещё не поднялись.
    Хоронить в этот момент нельзя — иначе снесём всю живую очередь."""
    await _seed(maker)
    async with maker() as s:
        assert await GlobalRepository(s).dead_letter_undeliverable([], "x") == 0
        await s.commit()
    assert all(v == "pending" for v in (await _statuses(maker)).values())


async def test_burial_is_idempotent(maker):
    """Повторный проход не хоронит то же самое снова и не трогает dead."""
    await _seed(maker)
    async with maker() as s:
        g = GlobalRepository(s)
        first = await g.dead_letter_undeliverable(["tg"], "причина")
        await s.commit()
        second = await g.dead_letter_undeliverable(["tg"], "причина")
        await s.commit()
    assert first == 2 and second == 0


async def test_queue_drains_so_age_alert_stops_lying(maker):
    """После похорон недоставляемых в pending остаётся только то, что
    реально можно отправить — алерт о возрасте очереди снова осмыслен."""
    await _seed(maker)
    async with maker() as s:
        g = GlobalRepository(s)
        await g.dead_letter_undeliverable(["tg", "vk"], "причина")
        await s.commit()
        pending = (await s.execute(select(Outbox.platform).where(
            Outbox.status == "pending"))).scalars().all()
    assert set(pending) == {"tg", "vk"}      # legacy похоронен


@pytest.mark.parametrize("connected", [["tg"], ["tg", "vk"]])
async def test_only_unconnected_platforms_are_buried(maker, connected):
    """Хороним ровно то, для чего нет sender: если VK не подключён, его
    сообщения тоже недоставляемы."""
    await _seed(maker)
    async with maker() as s:
        await GlobalRepository(s).dead_letter_undeliverable(connected, "причина")
        await s.commit()
    st = await _statuses(maker)
    for platform in ("tg", "vk", "legacy"):
        expected = "pending" if platform in connected else "dead"
        assert st[platform] == expected, f"{platform}: ожидали {expected}"
