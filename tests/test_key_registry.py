"""
Строгий реестр версионированных ключей: конфликты, недопустимые метки,
отсутствующая активная версия — отклоняются одинаково для телефонов и
токенов ботов.
"""
import pytest

from app.core import bot_tokens, phones
from app.core.keyring import KeyConfigError


@pytest.fixture(autouse=True)
def _clean_phone(monkeypatch):
    for attr, val in (("phone_keys", ""), ("phone_keyring", ""),
                      ("phone_enc_key", ""), ("phone_active_key_version", ""),
                      ("phone_legacy_versions", ""),
                      ("jwt_secret", "тест-jwt-секрет-достаточно-длинный")):
        monkeypatch.setattr(phones.settings, attr, val)
    for attr, val in (("bot_token_keys", ""), ("bot_token_keyring", ""),
                      ("bot_token_enc_key", ""),
                      ("bot_token_active_key_version", "")):
        monkeypatch.setattr(bot_tokens.settings, attr, val)


# ---------- телефоны ----------

def test_same_version_same_secret_ok(monkeypatch):
    monkeypatch.setattr(phones.settings, "phone_keyring", "v2:AAA")
    monkeypatch.setattr(phones.settings, "phone_keys", "v2:AAA")
    phones.assert_config_valid()          # не бросает


def test_same_version_different_secret_rejected(monkeypatch):
    monkeypatch.setattr(phones.settings, "phone_keyring", "v2:AAA")
    monkeypatch.setattr(phones.settings, "phone_keys", "v2:BBB")
    with pytest.raises(KeyConfigError) as e:
        phones.assert_config_valid()
    msg = str(e.value)
    assert "v2" in msg and "PHONE_KEYRING" in msg and "PHONE_KEYS" in msg
    assert "AAA" not in msg and "BBB" not in msg    # секреты не раскрыты


def test_explicit_overrides_jwt_default_without_conflict(monkeypatch):
    """Штатная ротация: старый JWT кладут в keyring под меткой jwt — это
    перекрывает выведенный из нового JWT_SECRET без конфликта."""
    monkeypatch.setattr(phones.settings, "jwt_secret", "новый-jwt-секрет-ок")
    monkeypatch.setattr(phones.settings, "phone_keyring", "jwt:старый-jwt")
    phones.assert_config_valid()
    assert phones._secret_for("jwt") == "старый-jwt"


def test_active_version_must_exist(monkeypatch):
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "v9")
    with pytest.raises(KeyConfigError) as e:
        phones.assert_config_valid()
    assert "v9" in str(e.value)


def test_bad_version_label_rejected(monkeypatch):
    monkeypatch.setattr(phones.settings, "phone_keys", "toolongversion:AAA")
    with pytest.raises(KeyConfigError):
        phones.assert_config_valid()


def test_bad_active_label_rejected(monkeypatch):
    monkeypatch.setattr(phones.settings, "phone_keys", "v2:AAA")
    monkeypatch.setattr(phones.settings, "phone_active_key_version", "v/2")
    with pytest.raises(KeyConfigError):
        phones.assert_config_valid()


# ---------- токены ботов ----------

def test_bot_conflict_rejected(monkeypatch):
    monkeypatch.setattr(bot_tokens.settings, "bot_token_keyring", "v1:AAA")
    monkeypatch.setattr(bot_tokens.settings, "bot_token_keys", "v1:BBB")
    with pytest.raises(KeyConfigError) as e:
        bot_tokens.assert_config_valid()
    assert "bot_token" in str(e.value)
    assert "AAA" not in str(e.value)


def test_bot_missing_active_rejected(monkeypatch):
    monkeypatch.setattr(bot_tokens.settings, "bot_token_keys", "v1:AAA")
    monkeypatch.setattr(bot_tokens.settings, "bot_token_active_key_version", "v5")
    with pytest.raises(KeyConfigError):
        bot_tokens.assert_config_valid()


def test_bot_no_keys_is_valid(monkeypatch):
    # шифрование токенов выключено — проверять нечего
    bot_tokens.assert_config_valid()
