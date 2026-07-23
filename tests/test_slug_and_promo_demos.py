"""
Короткий адрес клуба и витрина направлений на промо-странице.

Две связанные вещи:
  * `/c/salon-hortensia` вместо `/club/3` — ссылку печатают в QR и диктуют
    по телефону, и «слэш клуб слэш три» для этого не годится;
  * на промо показываем демо КАЖДОГО направления. Одного демо мало:
    владелец салона, увидев спортивную секцию, решает, что платформа не
    про него, и уходит.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

H = {"x-admin-token": "tok"}


@pytest.fixture(autouse=True)
def _clean():
    from app.api import rate_limit
    rate_limit._memory.clear()
    yield
    rate_limit._memory.clear()


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _make_club(c, name: str) -> int:
    return c.post("/api/tenants", json={"name": name}, headers=H).json()["id"]


def _set(tenant_id: int, **fields) -> None:
    """Правим клуб напрямую: у API нет полей slug/bot_username."""
    import asyncio

    async def _apply():
        from app.db.engine import SessionLocal, engine
        from app.models.entities import Tenant
        await engine.dispose()
        async with SessionLocal() as s:
            t = await s.get(Tenant, tenant_id)
            for k, v in fields.items():
                setattr(t, k, v)
            await s.commit()

    asyncio.run(_apply())


# ---------- короткий адрес ----------

def test_slug_opens_the_club_page(client):
    tid = _make_club(client, "Салон Гортензия")
    _set(tid, slug="salon-hortensia")
    r = client.get("/c/salon-hortensia", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"/club/{tid}"


def test_slug_actually_reaches_the_page(client):
    tid = _make_club(client, "Салон Следом")
    _set(tid, slug="salon-sledom")
    r = client.get("/c/salon-sledom")          # с переходом
    assert r.status_code == 200
    assert "Салон Следом" in r.text


def test_old_numeric_link_keeps_working(client):
    """По /club/<id> уже сделаны ссылки и QR — ломать их нельзя."""
    tid = _make_club(client, "Клуб Старый")
    _set(tid, slug="klub-stary")
    assert client.get(f"/club/{tid}").status_code == 200


def test_unknown_slug_is_404(client):
    assert client.get("/c/no-such-club").status_code == 404


def test_inactive_club_is_not_reachable_by_slug(client):
    """Выключенный клуб не должен открываться и по короткому адресу."""
    tid = _make_club(client, "Клуб Выключен")
    _set(tid, slug="klub-off", is_active=False)
    assert client.get("/c/klub-off", follow_redirects=False).status_code == 404


def test_qr_encodes_the_short_address(client):
    """QR печатают: он обязан кодировать тот адрес, который мы показываем."""
    tid = _make_club(client, "Клуб С Кодом")
    _set(tid, slug="klub-s-kodom")
    assert client.get(f"/club/{tid}/qr").status_code == 200


# ---------- витрина направлений ----------

def test_promo_lists_every_demo_direction(client):
    ids = []
    for name, vertical, slug, bot in (
        ("Демо спорт", "sport", "demo-sport", "DemoSportBot"),
        ("Демо салон", "beauty", "demo-salon", "DemoSalonBot"),
        ("Демо репетитор", "tutor", "demo-tutor", "DemoTutorBot"),
    ):
        tid = _make_club(client, name)
        _set(tid, vertical=vertical, slug=slug, bot_username=bot, is_demo=True)
        ids.append(tid)

    page = client.get("/promo").text
    for bot in ("DemoSportBot", "DemoSalonBot", "DemoTutorBot"):
        assert f"https://t.me/{bot}" in page, bot
    for slug in ("demo-sport", "demo-salon", "demo-tutor"):
        assert f"/c/{slug}" in page, slug
    # направления названы своими словами
    assert "Салон красоты" in page and "Репетиторы" in page

    for tid in ids:
        _set(tid, is_demo=False)          # не мешаем другим тестам


def test_promo_survives_demo_without_bot_username(client):
    """Без username бота карточка всё равно нужна — со ссылкой на страницу."""
    tid = _make_club(client, "Демо без бота")
    _set(tid, vertical="beauty", slug="demo-nobot", is_demo=True)
    other = _make_club(client, "Демо второе")
    _set(other, vertical="tutor", slug="demo-vtoroe", is_demo=True)

    page = client.get("/promo").text
    assert "/c/demo-nobot" in page
    assert "t.me/None" not in page and "t.me/\"" not in page

    _set(tid, is_demo=False)
    _set(other, is_demo=False)


def test_promo_without_demos_has_no_broken_links(client):
    page = client.get("/promo")
    assert page.status_code == 200
    assert "/club/None" not in page.text


# ---------- витрина: три карточки, в каждой обе кнопки ----------

class _FakeTenant:
    def __init__(self, tid, name, vertical, slug=None, bot=None):
        self.id, self.name, self.vertical = tid, name, vertical
        self.slug, self.bot_username, self.brand_name = slug, bot, None


def _cards_html() -> str:
    from app.api.promo_page import render_promo_page

    demos = [
        _FakeTenant(1, "Демо-клуб", "sport", "demo-sport", "DemoSportBot"),
        _FakeTenant(2, "Демо-салон", "beauty", "demo-salon", "DemoSalonBot"),
        _FakeTenant(3, "Демо-репетитор", "tutor", "demo-tutor", "DemoTutorBot"),
    ]
    page = render_promo_page(1, demos)
    start = page.index('<div class="demo-grid">')
    return page[start:page.index("</div>\n\n", start)]


def test_generic_bot_card_is_gone():
    """Посетитель выбирает не «бота вообще», а свой вид бизнеса."""
    assert "Демо-бот в Telegram" not in _cards_html()


def test_exactly_one_card_per_direction():
    html = _cards_html()
    assert html.count('class="demo-card"') == 3
    for tag in ("Спорт и тренировки", "Салон красоты", "Репетиторы и обучение"):
        assert tag in html, tag


def test_every_card_has_both_buttons():
    """Кнопка бота и кнопка страницы записи — в каждой карточке."""
    import re

    html = _cards_html()
    for card in html.split('<div class="demo-card">')[1:]:
        assert "Открыть бота" in card, card[:120]
        assert "Страница записи" in card, card[:120]
    bots = re.findall(r'href="(https://t\.me/[^"]+)"', html)
    assert sorted(bots) == ["https://t.me/DemoSalonBot",
                            "https://t.me/DemoSportBot",
                            "https://t.me/DemoTutorBot"]


def test_fallback_keeps_generic_bot_when_no_demos():
    """Пока демо не настроены, раздел не должен пустовать."""
    from app.api.promo_page import render_promo_page

    assert "Демо-бот в Telegram" in render_promo_page(None)


# ---------- выбор специалиста по вертикали ----------

@pytest.mark.parametrize("vertical,word", [
    ("sport", "тренера"), ("beauty", "мастера"), ("tutor", "преподавателя"),
])
def test_club_page_picks_the_right_specialist_word(client, vertical, word):
    """На спортивной и репетиторской странице стоял «выбор мастера»."""
    import datetime as dt

    tid = _make_club(client, f"Клуб {vertical}")
    _set(tid, vertical=vertical)
    client.post(f"/api/tenants/{tid}/masters", headers=H,
                json={"name": "Специалист", "specialty": "тест"})
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    client.post(f"/api/tenants/{tid}/trainings", headers=H, json={
        "title": "Слот", "start_at": start, "max_participants": 2})

    page = client.get(f"/club/{tid}").text
    assert f"Выбрать {word}" in page, f"{vertical}: нет «Выбрать {word}»"
    if vertical != "beauty":
        assert "Выбрать мастера" not in page
