"""
Демо-клубы наполняются по вертикали: спорт, салон, репетитор.

Демо-бот — витрина. Человек приходит посмотреть, подойдёт ли платформа
ЕМУ: владельцу салона тренировки в зале не говорят ничего. Поэтому у
каждой вертикали свой набор примеров, а сценарий «спорт» обязан остаться
ровно тем же, что был, — существующий демо-клуб не должен измениться.
"""
import pytest

from app.core.verticals import VERTICALS, vcfg
from app.services.demo_content import SCENARIOS, scenario


# ---------- вертикали ----------

@pytest.mark.parametrize("name", ["sport", "beauty", "tutor"])
def test_vertical_is_registered(name):
    assert name in VERTICALS


def test_tutor_speaks_its_own_language():
    v = vcfg("tutor")
    assert v["master_word"] == "преподаватель"
    assert "Занятия" in v["btn_list"]
    assert v["label"] == "Репетиторы и обучение"


def test_unknown_vertical_falls_back_to_sport():
    """Обратная совместимость: у существующих клубов поле может быть пустым."""
    assert vcfg(None) is VERTICALS["sport"]
    assert vcfg("") is VERTICALS["sport"]
    assert vcfg("нет-такой") is VERTICALS["sport"]


def test_every_vertical_has_full_terminology():
    """Пропущенный ключ обернулся бы KeyError уже в проде, на живом боте."""
    required = set(VERTICALS["sport"])
    for name, cfg in VERTICALS.items():
        assert required <= set(cfg), f"{name}: не хватает {required - set(cfg)}"


# ---------- сценарии демо ----------

@pytest.mark.parametrize("name", ["sport", "beauty", "tutor"])
def test_scenario_is_complete(name):
    s = scenario(name)
    assert s["masters"] and s["slots"]
    for key in ("about", "address", "contact_phone"):
        assert s["profile"][key]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_slots_reference_existing_masters(name):
    """coach — индекс в списке специалистов: выход за границы молча оставил
    бы слот без мастера, и витрина выглядела бы недоделанной."""
    s = scenario(name)
    for slot in s["slots"]:
        idx = slot.get("coach")
        if idx is not None:
            assert 0 <= idx < len(s["masters"]), f"{name}: coach={idx}"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_slots_are_sane(name):
    for slot in scenario(name)["slots"]:
        assert slot["max_participants"] >= 1
        assert slot["duration_min"] > 0
        assert 0 <= slot["hour"] <= 23
        assert slot["days"] >= 1, "слот в прошлом на витрине не нужен"
        assert slot["title"] and slot["location"]


def test_sport_scenario_is_unchanged():
    """Существующий демо-клуб не должен заметить появления вертикалей."""
    s = scenario("sport")
    assert [m["name"] for m in s["masters"]] == ["Алексей Морозов",
                                                 "Ирина Соколова"]
    assert [x["title"] for x in s["slots"]] == [
        "Вечерняя игра", "Утренняя тренировка", "Турнир выходного дня"]
    assert s["profile"]["address"] == "г. Москва, ул. Спортивная, 1"


def test_service_verticals_show_individual_booking():
    """Ключевое отличие салона и репетитора от спорта: запись бывает
    ИНДИВИДУАЛЬНОЙ. Если этого нет в демо, посетитель решит, что платформа
    умеет только группы."""
    for name in ("beauty", "tutor"):
        caps = [s["max_participants"] for s in scenario(name)["slots"]]
        assert 1 in caps, f"{name}: нет ни одного индивидуального времени"


def test_tutor_shows_both_individual_and_group():
    """Репетитору важны оба формата: занятие один на один и группа."""
    caps = [s["max_participants"] for s in scenario("tutor")["slots"]]
    assert 1 in caps and any(c > 1 for c in caps)


def test_scenarios_do_not_share_content():
    """Сценарии должны реально отличаться, а не быть копией спорта."""
    titles = {name: {s["title"] for s in scenario(name)["slots"]}
              for name in SCENARIOS}
    assert not titles["sport"] & titles["beauty"]
    assert not titles["sport"] & titles["tutor"]
    assert not titles["beauty"] & titles["tutor"]


def test_unknown_vertical_gets_sport_scenario():
    assert scenario(None) is SCENARIOS["sport"]
    assert scenario("нет-такой") is SCENARIOS["sport"]


def test_every_vertical_has_a_demo_scenario():
    """Иначе новая вертикаль молча получила бы спортивную витрину."""
    assert set(VERTICALS) == set(SCENARIOS)


# ---------- ночной пересбор применяет сценарий ----------

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.models.entities import Base, Master, Tenant, Training  # noqa: E402


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    from app.services import tasks
    monkeypatch.setattr(tasks, "SessionLocal", maker)
    yield maker
    await engine.dispose()


@pytest.mark.parametrize("vertical,expected_master,expected_slot", [
    ("sport", "Алексей Морозов", "Вечерняя игра"),
    ("beauty", "Марина Ковалёва", "Стрижка и укладка"),
    ("tutor", "Наталья Белова", "Математика: подготовка к ЕГЭ"),
])
async def test_nightly_reset_seeds_by_vertical(db, vertical, expected_master,
                                               expected_slot):
    from app.services.tasks import _demo_reset_daily

    async with db() as s:
        s.add(Tenant(name=f"Демо {vertical}", is_demo=True, vertical=vertical))
        await s.commit()

    await _demo_reset_daily([None])

    async with db() as s:
        masters = [m for (m,) in (await s.execute(select(Master.name))).all()]
        titles = [t for (t,) in (await s.execute(select(Training.title))).all()]
        tenant = (await s.execute(select(Tenant))).scalar_one()

    assert expected_master in masters, masters
    assert expected_slot in titles, titles
    assert tenant.about and "Демо" in tenant.about


async def test_demo_clubs_of_different_verticals_do_not_mix(db):
    """Два демо-клуба разных направлений пересобираются каждый по-своему."""
    from app.services.tasks import _demo_reset_daily

    async with db() as s:
        s.add(Tenant(name="Демо салон", is_demo=True, vertical="beauty"))
        s.add(Tenant(name="Демо репетитор", is_demo=True, vertical="tutor"))
        await s.commit()

    await _demo_reset_daily([None])

    async with db() as s:
        rows = (await s.execute(select(Training.tenant_id, Training.title))).all()
        tenants = {t.id: t.vertical
                   for t in (await s.execute(select(Tenant))).scalars()}

    by_vertical: dict[str, set[str]] = {}
    for tenant_id, title in rows:
        by_vertical.setdefault(tenants[tenant_id], set()).add(title)

    assert "Стрижка и укладка" in by_vertical["beauty"]
    assert "Математика: подготовка к ЕГЭ" in by_vertical["tutor"]
    assert not by_vertical["beauty"] & by_vertical["tutor"]


async def test_real_clubs_are_untouched_by_demo_reset(db):
    """Пересбор трогает только демо: у боевого клуба ничего не появляется."""
    from app.services.tasks import _demo_reset_daily

    async with db() as s:
        s.add(Tenant(name="Боевой клуб", is_demo=False, vertical="beauty"))
        await s.commit()

    await _demo_reset_daily([None])

    async with db() as s:
        assert (await s.execute(select(Training))).first() is None
        assert (await s.execute(select(Master))).first() is None
