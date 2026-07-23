"""
Синхронизация уже отправленных карточек между каналами.

Данные синхронизировать не нужно — база одна. Расходятся УЖЕ ОТПРАВЛЕННЫЕ
сообщения: они снимки и сами себя не перерисовывают.

Боевой пробел: запись через сайт обновляла карточку в TG-группе, но не
трогала VK-переписки. В VK адрес сообщения известен только в момент
отправки, поэтому раньше найти его было невозможно — теперь он сохраняется
(vk_cards), и карточку можно обновить из любого канала.
"""
import datetime as dt

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bots import vk
from app.models.entities import Base, Tenant, VkCard
from app.services.booking import BookingService


class _Messages:
    """Заглушка VK API: помнит отправленное и правки."""

    def __init__(self, fail_edit: Exception | None = None):
        self.sent, self.edited = [], []
        self.fail_edit = fail_edit
        self._next_id = 1000

    async def send(self, user_id=None, message=None, **kw):
        self._next_id += 1
        self.sent.append((user_id, message, self._next_id))
        return self._next_id

    async def edit(self, peer_id=None, message_id=None, message=None, **kw):
        if self.fail_edit:
            raise self.fail_edit
        self.edited.append((peer_id, message_id))
        return 1


class _Api:
    def __init__(self, fail_edit=None):
        self.messages = _Messages(fail_edit)


@pytest_asyncio.fixture
async def maker(monkeypatch):
    from sqlalchemy import event

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})

    # В SQLite внешние ключи по умолчанию ВЫКЛЮЧЕНЫ, и каскад молча не
    # сработает. Приложение включает их в app/db/engine.py — тест обязан
    # проверять ту же схему, иначе он врёт про каскад.
    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):        # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(vk, "SessionLocal", m)
    yield m
    await engine.dispose()


async def _club_and_slot(maker) -> tuple[int, int]:
    async with maker() as s:
        t = Tenant(name="Клуб ВК", vk_group_id=555)
        s.add(t)
        await s.commit()
        svc = BookingService(s, t.id)
        tr = await svc.repo.add_training(
            title="Занятие",
            start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
            location="Зал", max_participants=5, duration_min=60,
            state="published", publish_at=None,
            created_by_platform="test", created_by_id=0)
        await s.commit()
        return t.id, tr.id


def _use(monkeypatch, api, tenant_id):
    monkeypatch.setattr(vk, "_api", lambda: api)
    monkeypatch.setattr(vk, "_api_by_tenant", {tenant_id: api})
    monkeypatch.setattr(vk, "_configured_tenants", set())


# ---------- адрес карточки запоминается ----------

async def test_sent_card_is_remembered(maker, monkeypatch):
    tid, train_id = await _club_and_slot(maker)
    api = _Api()
    _use(monkeypatch, api, tid)

    await vk.send_card(777, tid, train_id, "карточка")

    async with maker() as s:
        rows = list((await s.execute(select(VkCard))).scalars())
    assert len(rows) == 1
    assert (rows[0].peer_id, rows[0].training_id) == (777, train_id)


async def test_repeat_send_replaces_address(maker, monkeypatch):
    """Одна строка на (человек, занятие) — иначе таблица растёт без границ
    и мы правим одно и то же сообщение по нескольку раз."""
    tid, train_id = await _club_and_slot(maker)
    api = _Api()
    _use(monkeypatch, api, tid)

    await vk.send_card(777, tid, train_id, "первая")
    await vk.send_card(777, tid, train_id, "вторая")

    async with maker() as s:
        rows = list((await s.execute(select(VkCard))).scalars())
    assert len(rows) == 1
    assert rows[0].message_id == api.messages.sent[-1][2]


# ---------- обновление из другого канала ----------

async def test_refresh_updates_every_saved_card(maker, monkeypatch):
    """Ради этого всё и делалось: запись с сайта перерисовывает карточки
    у всех, кто их получил в VK."""
    tid, train_id = await _club_and_slot(maker)
    api = _Api()
    _use(monkeypatch, api, tid)
    for uid in (101, 102, 103):
        await vk.send_card(uid, tid, train_id, "карточка")

    updated = await vk.refresh_cards(tid, train_id)

    assert updated == 3
    assert sorted(p for p, _ in api.messages.edited) == [101, 102, 103]


async def test_refresh_without_cards_is_noop(maker, monkeypatch):
    """У клуба может не быть VK вовсе — это не ошибка."""
    tid, train_id = await _club_and_slot(maker)
    api = _Api()
    _use(monkeypatch, api, tid)
    assert await vk.refresh_cards(tid, train_id) == 0
    assert api.messages.edited == []


async def test_dead_card_address_is_dropped(maker, monkeypatch):
    """Сообщение удалено или слишком старое для правки — адрес больше не
    нужен, иначе мы будем дёргать VK впустую вечно."""
    tid, train_id = await _club_and_slot(maker)
    ok_api = _Api()
    _use(monkeypatch, ok_api, tid)
    await vk.send_card(777, tid, train_id, "карточка")

    broken = _Api(fail_edit=RuntimeError("message not found"))
    _use(monkeypatch, broken, tid)
    assert await vk.refresh_cards(tid, train_id) == 0

    async with maker() as s:
        assert (await s.execute(select(VkCard))).first() is None


async def test_cards_die_with_the_training(maker, monkeypatch):
    """Каскад: осиротевшие адреса указывали бы в пустоту."""
    from app.models.entities import Training

    tid, train_id = await _club_and_slot(maker)
    api = _Api()
    _use(monkeypatch, api, tid)
    await vk.send_card(777, tid, train_id, "карточка")

    async with maker() as s:
        await s.delete(await s.get(Training, train_id))
        await s.commit()
        assert (await s.execute(select(VkCard))).first() is None


# ---------- единая точка синхронизации ----------

async def test_notify_slot_changed_touches_both_channels(monkeypatch):
    """Любое изменение обязано звать один вызов, а он — все каналы."""
    from app.services import card_sync

    called = []
    async def _tg(t, s): called.append(("tg", t, s))
    async def _vk(t, s): called.append(("vk", t, s))
    monkeypatch.setattr(card_sync, "_refresh_telegram", _tg)
    monkeypatch.setattr(card_sync, "_refresh_vk", _vk)

    await card_sync.notify_slot_changed(7, 42)
    assert called == [("tg", 7, 42), ("vk", 7, 42)]


async def test_one_broken_channel_does_not_stop_the_other(monkeypatch):
    """Обновление карточки — украшение поверх уже совершённой записи.
    Недоступный Telegram не должен мешать обновить VK."""
    from app.services import card_sync

    called = []
    async def _tg(t, s): raise RuntimeError("telegram down")
    async def _vk(t, s): called.append("vk")
    monkeypatch.setattr(card_sync, "_refresh_telegram", _tg)
    monkeypatch.setattr(card_sync, "_refresh_vk", _vk)

    await card_sync.notify_slot_changed(7, 42)      # не бросает
    assert called == ["vk"]
