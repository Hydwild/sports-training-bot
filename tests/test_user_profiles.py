"""username и photo_url сохраняются и видны в карточке тренировки."""
import datetime as dt
import pytest
from app.repositories.repo import GlobalRepository
from app.services.booking import BookingService
from app.bots.views import _label


async def _club(session):
    g = GlobalRepository(session)
    t = await g.create_tenant(name="Клуб")
    await session.commit()
    return t.id


async def test_signup_saves_username(session):
    tid = await _club(session)
    svc = BookingService(session, tid)
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    tr = await svc.create_training(title="T", start_at=now, location="",
                                   max_participants=5, platform="tg", user_id=1)
    await svc.sign_up(tr.id, "tg", 100, "Аня Иванова", username="anya_iv")
    s = await svc.repo.get_user_signup(tr.id, "tg", 100)
    assert s.username == "anya_iv"


async def test_subscriber_saves_photo_url(session):
    tid = await _club(session)
    svc = BookingService(session, tid)
    await svc.repo.upsert_subscriber("tg", 100, "Аня",
                                     username="anya_iv",
                                     photo_url="https://t.me/photo.jpg")
    await session.commit()
    from app.models.entities import Subscriber
    from sqlalchemy import select
    sub = (await session.execute(
        select(Subscriber).where(Subscriber.user_id == 100)
    )).scalar_one_or_none()
    assert sub.username == "anya_iv"
    assert sub.photo_url == "https://t.me/photo.jpg"


def test_label_shows_username():
    class FakeSignup:
        name = "Аня Иванова"
        username = "anya_iv"
        is_guest = False
    assert "@anya_iv" in _label(FakeSignup())


def test_label_without_username():
    class FakeSignup:
        name = "Боря"
        username = None
        is_guest = False
    assert _label(FakeSignup()) == "Боря"


def test_label_guest_unconfirmed():
    class FakeSignup:
        name = "Гость"
        username = None
        is_guest = True
        confirmed = False
    assert "требует подтверждения" in _label(FakeSignup())


# ---------- VK профиль ----------

def test_vk_profile_link_with_custom_screen():
    from app.bots.user_info import profile_link
    assert profile_link("badminton_fan", 12345, "vk") == "https://vk.com/badminton_fan"


def test_vk_profile_link_fallback_to_id():
    from app.bots.user_info import profile_link
    assert profile_link(None, 12345, "vk") == "https://vk.com/id12345"


def test_vk_technical_screen_name_hidden():
    """screen_name вида id123456 не должен показываться — это не настоящий никнейм."""
    import asyncio
    from app.bots.user_info import fetch_vk_profile

    class FakeUser:
        first_name = "Вася"
        last_name = "Пупкин"
        screen_name = "id99999"   # технический, без пользовательского никнейма
        photo_200 = "https://vk.com/photo.jpg"

    class FakeAPI:
        class users:
            @staticmethod
            async def get(user_ids, fields):
                return [FakeUser()]

    profile = asyncio.run(
        fetch_vk_profile(FakeAPI(), 99999))
    assert profile.name == "Вася Пупкин"
    assert profile.username is None          # технический screen_name скрыт
    assert profile.photo_url == "https://vk.com/photo.jpg"


# ---------- Экранирование HTML в карточке тренировки ----------

async def test_training_card_escapes_malicious_display_name():
    """Регресс: Telegram позволяет пользователю задать в имени профиля любые
    символы, включая теги. training_card() отправляется с parse_mode="HTML" —
    без экранирования участник мог внедрить кликабельную ссылку/разметку в
    карточку, которую видят все в группе (и тренер)."""
    from app.bots.views import training_card

    class FakeTraining:
        id = 1
        title = "Тренировка"
        location = ""
        max_participants = 5
        duration_min = 90
        price_minor = 0
        state = "published"
        publish_at = None
        start_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)

    class FakeSignup:
        id = 1
        name = '<a href="https://evil.example/phish">🎁 Приз</a>'
        username = None
        is_guest = False
        user_id = 999
        confirmed = True

    class FakeRepo:
        async def get_signups(self, tid, status):
            return [FakeSignup()] if status == "active" else []

        async def aliases_map(self, platform):
            return {}

    class FakeSvc:
        repo = FakeRepo()

        def format_local(self, when):
            return "01.01.2026 19:00"

    card = await training_card(FakeSvc(), FakeTraining())
    assert "<a href=" not in card          # тег не прошёл как есть
    assert "&lt;a href=" in card           # экранирован
    assert "evil.example" in card          # текст остался виден, просто безопасен


def test_vk_real_screen_name_kept():
    import asyncio
    from app.bots.user_info import fetch_vk_profile

    class FakeUser:
        first_name = "Аня"
        last_name = ""
        screen_name = "anya_badminton"
        photo_200 = None

    class FakeAPI:
        class users:
            @staticmethod
            async def get(user_ids, fields):
                return [FakeUser()]

    profile = asyncio.run(
        fetch_vk_profile(FakeAPI(), 111))
    assert profile.username == "anya_badminton"
    assert profile.photo_url is None
