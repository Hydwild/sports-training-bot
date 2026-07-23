"""
Своя ссылка на страницу записи у каждого клиента.

У клуба может быть собственный сайт или домен. Тогда именно его адрес
обязан уходить в QR-код (его печатают и вешают в зале), в кнопку бота и в
список клиентов — иначе распечатанный код ведёт не туда, куда клиент
рассчитывает.

Адрес попадает в inline-кнопку Telegram и в QR, поэтому принимается только
https и только внешний хост: `javascript:` в кнопке — это XSS у всех, кто
её нажмёт, а внутренний адрес в QR уводит посетителя во внутреннюю сеть.
"""
import pytest

from app.core.club_url import (
    club_site_url,
    club_site_url_or_none,
    validate_site_url,
)
from app.core.config import settings


class _Tenant:
    def __init__(self, tenant_id: int = 1, site_url: str | None = None):
        self.id = tenant_id
        self.site_url = site_url


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://neomeal.example")


# ---------- выбор адреса ----------

def test_default_is_our_club_page():
    assert club_site_url(_Tenant(7)) == "https://neomeal.example/club/7"


def test_custom_url_wins():
    t = _Tenant(7, "https://salon-hortensia.ru/zapis")
    assert club_site_url(t) == "https://salon-hortensia.ru/zapis"


def test_blank_custom_url_falls_back(monkeypatch):
    """Пустая строка и пробелы — это «не задано», а не «пустая ссылка»."""
    assert club_site_url(_Tenant(7, "   ")) == "https://neomeal.example/club/7"
    assert club_site_url(_Tenant(7, "")) == "https://neomeal.example/club/7"


def test_custom_url_works_without_public_base_url(monkeypatch):
    """Свой адрес самодостаточен: PUBLIC_BASE_URL для него не нужен."""
    monkeypatch.setattr(settings, "public_base_url", "")
    t = _Tenant(7, "https://salon.ru/zapis")
    assert club_site_url(t) == "https://salon.ru/zapis"


def test_no_url_at_all_raises(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")
    with pytest.raises(RuntimeError):
        club_site_url(_Tenant(7))
    assert club_site_url_or_none(_Tenant(7)) is None


# ---------- валидация ----------

def test_empty_is_allowed_and_means_default():
    assert validate_site_url("") is None
    assert validate_site_url(None) is None
    assert validate_site_url("   ") is None


def test_https_url_is_accepted():
    assert validate_site_url(" https://salon.ru/zapis ") == \
        "https://salon.ru/zapis"


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",          # XSS у каждого, кто нажмёт кнопку
    "data:text/html,<script>",
    "http://salon.ru",              # без TLS: ссылку можно подменить
    "salon.ru",                     # вообще не URL
    "file:///etc/passwd",
])
def test_dangerous_schemes_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_site_url(bad)


@pytest.mark.parametrize("internal", [
    "https://localhost/zapis",
    "https://127.0.0.1/zapis",
    "https://192.168.0.10/zapis",
    "https://169.254.169.254/latest/meta-data",   # метаданные облака
])
def test_internal_hosts_are_rejected(internal):
    """QR с внутренним адресом уводит посетителя во внутреннюю сеть."""
    with pytest.raises(ValueError):
        validate_site_url(internal)


def test_too_long_url_is_rejected():
    with pytest.raises(ValueError):
        validate_site_url("https://salon.ru/" + "x" * 600)


# ---------- короткий адрес на нашем домене ----------

from app.core.club_url import (  # noqa: E402
    RESERVED_SLUGS,
    bot_link,
    club_path,
    validate_bot_username,
    validate_slug,
)


class _Slugged(_Tenant):
    def __init__(self, tenant_id=1, slug=None, site_url=None,
                 bot_username=None):
        super().__init__(tenant_id, site_url)
        self.slug = slug
        self.bot_username = bot_username


def test_path_is_numeric_without_slug():
    assert club_path(_Slugged(3)) == "/club/3"


def test_slug_makes_the_path_readable():
    assert club_path(_Slugged(3, "salon-hortensia")) == "/c/salon-hortensia"


def test_slug_is_used_in_the_absolute_url():
    t = _Slugged(3, "salon-hortensia")
    assert club_site_url(t) == "https://neomeal.example/c/salon-hortensia"


def test_custom_site_url_still_wins_over_slug():
    """Свой сайт клиента важнее нашего короткого адреса."""
    t = _Slugged(3, "salon-hortensia", site_url="https://salon.ru/zapis")
    assert club_site_url(t) == "https://salon.ru/zapis"


@pytest.mark.parametrize("value,expected", [
    ("salon-hortensia", "salon-hortensia"),
    ("  Salon-Hortensia  ", "salon-hortensia"),   # регистр и пробелы
    ("/salon", "salon"),                          # ведущий слэш
    ("club2024", "club2024"),
])
def test_valid_slugs(value, expected):
    assert validate_slug(value) == expected


@pytest.mark.parametrize("bad", [
    "12345",              # выглядит как id — путало бы с /club/<id>
    "-salon",             # дефис по краям
    "salon-",
    "sa",                 # слишком коротко
    "салон",              # кириллица не читается в URL
    "salon hortensia",    # пробел
    "salon/zapis",        # слэш сломал бы маршрут
    "x" * 41,
])
def test_invalid_slugs_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_slug(bad)


@pytest.mark.parametrize("reserved", sorted(RESERVED_SLUGS))
def test_reserved_slugs_are_rejected(reserved):
    """Занять «club» или «admin» значит увести посетителя не туда."""
    with pytest.raises(ValueError):
        validate_slug(reserved)


def test_empty_slug_means_default():
    assert validate_slug("") is None and validate_slug(None) is None


# ---------- ссылка на бота ----------

@pytest.mark.parametrize("value,expected", [
    ("MyClubBot", "MyClubBot"),
    ("@MyClubBot", "MyClubBot"),
    ("https://t.me/MyClubBot", "MyClubBot"),
    ("t.me/MyClubBot", "MyClubBot"),
])
def test_bot_username_is_normalised(value, expected):
    assert validate_bot_username(value) == expected


@pytest.mark.parametrize("bad", ["bot", "имя_бота", "bot name", "b" * 40])
def test_bad_bot_username_rejected(bad):
    with pytest.raises(ValueError):
        validate_bot_username(bad)


def test_bot_link_built_from_username():
    assert bot_link(_Slugged(1, bot_username="MyClubBot")) == \
        "https://t.me/MyClubBot"
    assert bot_link(_Slugged(1)) is None
