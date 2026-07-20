"""
Регресс: подписи участников в карточке тренера должны собираться по ВСЕМ
платформам. Раньше training_card брала aliases_map("tg"), а web-записи
хранят подпись (с телефоном) под платформой "web" — тренер видел телефон
только в разделе «Имена», но не в общем списке карточки. То же для
VK-подписей в TG-карточке и наоборот.
"""
import datetime as dt

from app.bots import views
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService


async def _setup(session):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб Подписей")
    await session.commit()
    svc = BookingService(session, t.id)
    tr = await svc.create_training(
        title="Игра", start_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
        location="Зал", max_participants=8, platform="tg", user_id=1)
    return svc, tr


async def test_web_signup_phone_visible_in_admin_card(session):
    svc, tr = await _setup(session)
    # как делает public_signup: подписчик web + подпись с телефоном
    await svc.repo.upsert_subscriber("web", 79120001122, "Олег")
    await svc.repo.set_alias("web", 79120001122, "Олег 📱+79120001122")
    await svc.sign_up(tr.id, "web", 79120001122, "Олег")
    await session.commit()

    card_admin = await views.training_card(svc, tr, for_admin=True)
    assert "+79120001122" in card_admin  # телефон виден тренеру в общем списке

    card_group = await views.training_card(svc, tr, for_admin=False)
    assert "79120001122" not in card_group  # но НЕ виден в групповой карточке


async def test_cross_platform_aliases_in_admin_card(session):
    """VK-подпись тоже должна попадать в TG-карточку тренера (и наоборот) —
    список участников тренировки кросс-платформенный."""
    svc, tr = await _setup(session)
    await svc.repo.upsert_subscriber("vk", 555, "Vk User")
    await svc.repo.set_alias("vk", 555, "Петя (аренда корта)")
    await svc.sign_up(tr.id, "vk", 555, "Vk User")
    await session.commit()

    card_admin = await views.training_card(svc, tr, for_admin=True)
    assert "Петя (аренда корта)" in card_admin


async def test_plain_card_shows_all_platform_aliases(session):
    """VK-карточка тренера (training_card_plain + aliases_map_all): подпись
    web-участника с телефоном видна."""
    svc, tr = await _setup(session)
    await svc.repo.upsert_subscriber("web", 79995554433, "Мария")
    await svc.repo.set_alias("web", 79995554433, "Мария 📱+79995554433")
    await svc.sign_up(tr.id, "web", 79995554433, "Мария")
    await session.commit()

    aliases = await svc.repo.aliases_map_all()
    card = await views.training_card_plain(svc, tr, aliases)
    assert "+79995554433" in card

    # без подписей (карточка в группе VK) телефона нет
    card_public = await views.training_card_plain(svc, tr, None)
    assert "79995554433" not in card_public


async def test_alias_does_not_leak_across_users(session):
    """Подпись одного участника не должна подменять имя другого с тем же
    user_id на другой платформе."""
    svc, tr = await _setup(session)
    await svc.repo.upsert_subscriber("vk", 777, "Vk 777")
    await svc.repo.set_alias("vk", 777, "VK-подпись")
    await svc.sign_up(tr.id, "tg", 777, "Tg 777")  # тот же id, но платформа tg
    await session.commit()

    card_admin = await views.training_card(svc, tr, for_admin=True)
    assert "Tg 777" in card_admin
    assert "VK-подпись" not in card_admin
