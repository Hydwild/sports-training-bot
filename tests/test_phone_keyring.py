"""
Версии ключей телефонов и переход между ними.

Раньше индекс поиска считался ТЕКУЩИМ ключом: стоило добавить
PHONE_ENC_KEY — и существующий клиент переставал находиться, а на том же
номере заводился дубль. После ротации JWT_SECRET строки версии `jwt`
переставали расшифровываться совсем.
"""
import pytest

from app.core import phones

PHONE = "79161234567"
OLD_JWT = "старый-jwt-секрет-достаточной-длины"
NEW_KEY = "выделенный-ключ-телефонов-v1"


@pytest.fixture
def legacy_only(monkeypatch):
    """Состояние «до перехода»: выделенного ключа нет, версия jwt."""
    monkeypatch.setattr(phones.settings, "phone_enc_key", "")
    monkeypatch.setattr(phones.settings, "phone_keyring", "")
    monkeypatch.setattr(phones.settings, "jwt_secret", OLD_JWT)


def test_active_version_switches_only_with_dedicated_key(legacy_only,
                                                         monkeypatch):
    assert phones.active_key_ver() == phones.KEY_JWT
    monkeypatch.setattr(phones.settings, "phone_enc_key", NEW_KEY)
    assert phones.active_key_ver() == phones.KEY_V1


def test_legacy_row_still_found_after_adding_new_key(legacy_only, monkeypatch):
    """Ключевой регресс: иначе клиент «пропадает» и создаётся дубль."""
    legacy_index = phones.phone_index(PHONE)
    legacy_enc, legacy_ver = phones.encrypt(PHONE)
    assert legacy_ver == phones.KEY_JWT

    # добавили выделенный ключ — активной стала v1
    monkeypatch.setattr(phones.settings, "phone_enc_key", NEW_KEY)
    assert phones.active_key_ver() == phones.KEY_V1
    assert phones.phone_index(PHONE) != legacy_index      # новый индекс иной

    # но поиск проверяет и старую версию
    candidates = dict((v, i) for v, i in phones.index_candidates(PHONE))
    assert candidates[phones.KEY_JWT] == legacy_index
    # и старый шифротекст по-прежнему читается
    assert phones.decrypt(legacy_enc, legacy_ver) == PHONE


def test_jwt_rotation_does_not_break_old_rows(legacy_only, monkeypatch):
    """После ротации JWT прежнее значение живёт в PHONE_KEYRING."""
    legacy_enc, _ = phones.encrypt(PHONE)
    legacy_index = phones.phone_index(PHONE)

    # переход завершён не полностью: ключ добавлен, JWT уже сменили
    monkeypatch.setattr(phones.settings, "phone_enc_key", NEW_KEY)
    monkeypatch.setattr(phones.settings, "jwt_secret", "совсем-другой-секрет")
    monkeypatch.setattr(phones.settings, "phone_keyring", f"jwt:{OLD_JWT}")

    assert phones.decrypt(legacy_enc, phones.KEY_JWT) == PHONE
    assert dict(phones.index_candidates(PHONE))[phones.KEY_JWT] == legacy_index


def test_rotated_jwt_without_keyring_loses_old_rows_loudly(legacy_only,
                                                           monkeypatch):
    """Без связки старые строки не читаются — но данные не портятся и
    приложение не падает."""
    legacy_enc, _ = phones.encrypt(PHONE)
    monkeypatch.setattr(phones.settings, "jwt_secret", "совсем-другой-секрет")
    assert phones.decrypt(legacy_enc, phones.KEY_JWT) == ""


def test_missing_v1_key_is_explicit_not_silent(legacy_only):
    """Подставлять другой ключ вместо нужного нельзя: это либо «клиент не
    найден», либо дубль."""
    with pytest.raises(phones.KeyUnavailable):
        phones.phone_index(PHONE, phones.KEY_V1)
    with pytest.raises(phones.KeyUnavailable):
        phones.encrypt(PHONE, "неизвестная-версия")


def test_decrypt_with_unknown_version_returns_empty(legacy_only):
    enc, _ = phones.encrypt(PHONE)
    assert phones.decrypt(enc, "нет-такой") == ""


def test_index_is_stable_within_a_version(legacy_only):
    a = phones.phone_index("+7 916 123-45-67")
    b = phones.phone_index(PHONE)
    assert a == b
    assert phones.phone_index("79160000000") != a
