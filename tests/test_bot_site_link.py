"""
Из бота можно перейти на публичную страницу записи.

Бот показывает продукт со стороны клуба, а половина ценности — в том, как
всё выглядит у КЛИЕНТА в браузере: витрина, фото, мастера, выбор времени.
Без явной ссылки эту часть демо просто не находят.

Telegram принимает в кнопке только абсолютный http(s)-адрес, поэтому всё
завязано на PUBLIC_BASE_URL: подставлять host из запроса нельзя, его
задаёт клиент.
"""
import pytest

from app.bots import telegram as tg
from app.core.config import settings


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://neomeal.example")


def _urls(kb) -> list[str]:
    return [b.url for row in kb.inline_keyboard for b in row if b.url]


def _labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def test_site_button_points_to_the_given_url():
    kb = tg._site_kb("https://neomeal.example/club/7", "sport")
    assert _urls(kb) == ["https://neomeal.example/club/7"]


@pytest.mark.parametrize("vertical,expected", [
    ("sport", "Запись на тренировки"),
    ("beauty", "Онлайн-запись"),
    ("tutor", "Запись на занятия"),
])
def test_button_label_matches_the_vertical(vertical, expected):
    """Салону не нужна кнопка «Запись на тренировки»."""
    kb = tg._site_kb("https://neomeal.example/club/1", vertical)
    assert any(expected in label for label in _labels(kb))


def test_no_button_without_url():
    """Без адреса кнопку не рисуем: Telegram отвергает относительный URL,
    а host из запроса подставлять нельзя — его задаёт клиент."""
    assert tg._site_kb(None, "sport") is None
    assert tg._site_row(None, "sport") is None
    assert tg._site_kb("", "sport") is None


# ---------- демо: выбор роли ----------

def test_demo_role_keyboard_offers_the_site():
    kb = tg._demo_role_kb("https://neomeal.example/club/3", "beauty")
    data = [b.callback_data for row in kb.inline_keyboard for b in row
            if b.callback_data]
    assert "demo:coach" in data and "demo:participant" in data
    # витрину можно посмотреть, не выбирая роль
    assert _urls(kb) == ["https://neomeal.example/club/3"]


def test_demo_role_keyboard_survives_missing_url():
    """Без адреса выбор роли обязан работать по-прежнему."""
    kb = tg._demo_role_kb(None, "beauty")
    data = [b.callback_data for row in kb.inline_keyboard for b in row
            if b.callback_data]
    assert "demo:coach" in data
    assert _urls(kb) == []


def test_demo_role_keyboard_without_tenant_is_plain():
    """Совместимость со старым вызовом без аргументов."""
    assert _urls(tg._demo_role_kb()) == []


# ---------- приглашение после выбора роли ----------

class _Msg:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))


async def test_offer_site_is_worded_for_the_coach():
    m = _Msg()
    await tg._offer_site(m, "https://neomeal.example/club/5", "sport",
                         as_coach=True)
    text, kb = m.sent[0]
    assert "клиенты" in text
    assert _urls(kb) == ["https://neomeal.example/club/5"]


async def test_offer_site_is_worded_for_the_participant():
    m = _Msg()
    await tg._offer_site(m, "https://neomeal.example/club/5", "beauty",
                         as_coach=False)
    text, kb = m.sent[0]
    assert "Записаться" in text
    assert _urls(kb) == ["https://neomeal.example/club/5"]


async def test_offer_site_stays_silent_without_url():
    """Лучше промолчать, чем прислать нерабочую кнопку."""
    m = _Msg()
    await tg._offer_site(m, None, "sport", as_coach=False)
    assert m.sent == []
