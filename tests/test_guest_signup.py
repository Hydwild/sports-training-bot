"""Запись гостя за другого: занятие места, подтверждение, отклонение с подъёмом."""
import datetime as dt
import pytest
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService


async def _club_training(session, maxp):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб")
    await session.commit()
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    svc = BookingService(session, t.id)
    tr = await svc.create_training(title="T", start_at=now, location="",
                                   max_participants=maxp, platform="tg", user_id=1)
    return svc, tr.id


async def test_guest_takes_active_slot_unconfirmed(session):
    svc, tid = await _club_training(session, maxp=5)
    res = await svc.sign_up_guest(tid, "Петя (гость)", added_by=100)
    assert res.result == "active"
    guests = await svc.list_unconfirmed_guests(tid)
    assert len(guests) == 1
    assert guests[0].is_guest and guests[0].confirmed is False
    assert guests[0].name == "Петя (гость)"


async def test_guest_confirm(session):
    svc, tid = await _club_training(session, maxp=5)
    await svc.sign_up_guest(tid, "Гость", added_by=100)
    g = (await svc.list_unconfirmed_guests(tid))[0]
    confirmed = await svc.confirm_guest(g.id)
    assert confirmed.confirmed is True
    assert await svc.list_unconfirmed_guests(tid) == []


async def test_guest_reject_frees_slot_and_promotes(session):
    svc, tid = await _club_training(session, maxp=1)
    # реальный участник занимает единственное место
    await svc.sign_up(tid, "tg", 100, "Аня")
    # гость встаёт в очередь
    res = await svc.sign_up_guest(tid, "Гость", added_by=100)
    assert res.result == "queue"
    # теперь освободим активного и поставим гостя в актив, чтобы проверить
    # отклонение активного гостя с подъёмом очереди
    await svc.cancel_signup(tid, "tg", 100)   # гость поднимается в active
    g = (await svc.list_unconfirmed_guests(tid))[0]
    assert g.status == "active"
    # запишем ещё одного в очередь
    await svc.sign_up(tid, "tg", 200, "Боря")
    # отклоняем гостя -> место освобождается, Боря поднимается
    rej = await svc.reject_guest(g.id)
    assert rej["rejected"] is True
    assert rej["promoted"].name == "Боря"
    # training_id нужен вызывающему коду, чтобы обновить карточку в группе
    assert rej["training_id"] == tid


async def test_guest_isolated_per_tenant(session):
    svc_a, tid_a = await _club_training(session, maxp=5)
    await svc_a.sign_up_guest(tid_a, "Гость А", added_by=1)
    # другой клуб
    g = GlobalRepository(session)
    tb = await g.create_tenant(name="Клуб Б")
    await session.commit()
    svc_b = BookingService(session, tb.id)
    # клуб Б не видит гостя клуба А
    assert await svc_b.repo.get_training(tid_a) is None


async def test_guest_rapid_signups_no_uid_collision(session):
    """Регресс: раньше guest_uid строился из времени в мс, и несколько
    гостей, добавленных в одну и ту же миллисекунду (двойной тап/ретрай
    сети), могли получить одинаковый id и упасть с IntegrityError по
    уникальному индексу (tenant_id, training_id, platform, user_id)."""
    svc, tid = await _club_training(session, maxp=20)
    for i in range(15):
        res = await svc.sign_up_guest(tid, f"Гость {i}", added_by=1)
        assert res.result == "active"
    guests = await svc.list_unconfirmed_guests(tid)
    assert len(guests) == 15
    assert len({g.user_id for g in guests}) == 15  # все id уникальны
