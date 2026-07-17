"""
_resolve_tenant в vk.py: fail-closed вместо угадывания клуба.

Регресс: раньше при неизвестном group_id (или известном, но не совпавшем
ни с одним клубом) функция подставляла "первый клуб с любым vk_group_id"
вместо честного "не знаю". В мультиклиентной установке (несколько клубов
с VK) это могло привести к утечке данных не того клуба при временном
сбое определения group_id на старте бота.
"""
import pytest

from app.bots.vk import _resolve_tenant


@pytest.fixture(autouse=True)
def _reset_group_id(monkeypatch):
    # изолируем тесты от глобального _group_id, который выставляет setup()
    import app.bots.vk as vk_module
    monkeypatch.setattr(vk_module, "_group_id", None)


async def _mk_tenant(session, name, vk_group_id=None):
    from app.repositories.repo import GlobalRepository
    g = GlobalRepository(session)
    t = await g.create_tenant(name=name)
    if vk_group_id is not None:
        t.vk_group_id = vk_group_id
    await session.commit()
    return t


async def test_known_gid_matches_correct_tenant(session):
    await _mk_tenant(session, "А", vk_group_id=111)
    tb = await _mk_tenant(session, "Б", vk_group_id=222)
    found = await _resolve_tenant(session, 222)
    assert found is not None and found.id == tb.id


async def test_known_gid_no_match_fails_closed(session):
    """Раньше здесь подставлялся первый попавшийся клуб — это и была дыра."""
    await _mk_tenant(session, "А", vk_group_id=111)
    await _mk_tenant(session, "Б", vk_group_id=222)
    # group_id из события не совпадает ни с одним клубом
    found = await _resolve_tenant(session, 999)
    assert found is None


async def test_unknown_gid_single_candidate_resolves(session):
    """Group_id неизвестен, но во всей базе только один клуб с VK — это он."""
    ta = await _mk_tenant(session, "Единственный", vk_group_id=555)
    found = await _resolve_tenant(session, None)
    assert found is not None and found.id == ta.id


async def test_unknown_gid_multiple_candidates_fails_closed(session):
    """Group_id неизвестен, кандидатов несколько — раньше отдавался первый
    попавшийся (риск утечки данных не того клуба), теперь — None."""
    await _mk_tenant(session, "А", vk_group_id=111)
    await _mk_tenant(session, "Б", vk_group_id=222)
    found = await _resolve_tenant(session, None)
    assert found is None


async def test_unknown_gid_no_candidates_returns_none(session):
    await _mk_tenant(session, "Без VK")  # vk_group_id не задан
    found = await _resolve_tenant(session, None)
    assert found is None
