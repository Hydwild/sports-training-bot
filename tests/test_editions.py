"""Редакции Lite/Pro: feature-флаги переключают доступность функций."""
import importlib
import pytest


def _reload_with_edition(edition):
    """Перезагружает config и features с заданной редакцией."""
    import os
    os.environ["EDITION"] = edition
    from app.core import config as cfg
    cfg.get_settings.cache_clear()
    importlib.reload(cfg)
    from app.core import features as feat
    importlib.reload(feat)
    return feat.features


def test_lite_disables_pro_features():
    f = _reload_with_edition("lite")
    # Lite-функции — всегда
    assert f.signup and f.queue and f.reminders and f.attendance
    # Pro-функции — выключены
    assert f.payments is False
    assert f.statistics is False
    assert f.exports is False
    assert f.groups is False
    assert f.multi_tenant is False
    assert f.web_admin is False
    assert f.edition_name == "Lite"


def test_pro_enables_all():
    f = _reload_with_edition("pro")
    assert f.payments and f.statistics and f.exports
    assert f.groups and f.multi_tenant and f.web_admin and f.white_label
    assert f.edition_name == "Pro"


@pytest.fixture(autouse=True)
def _restore_edition():
    yield
    # вернуть pro по умолчанию, чтобы не влиять на другие тесты
    _reload_with_edition("pro")
