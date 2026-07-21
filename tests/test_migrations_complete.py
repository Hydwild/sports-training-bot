"""Страж полноты миграций.

Схема в проде долго держалась на Base.metadata.create_all при старте, и
часть таблиц (schedules) и колонок в историю alembic не попала: `alembic
upgrade head` на чистой базе давал НЕПОЛНУЮ схему. Тест ловит расхождение
сразу — иначе оно всплывает при первом развёртывании с нуля.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _alembic(args: list[str], db_path: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "ADMIN_API_TOKEN": "tok",
        "JWT_SECRET": "test-secret",
        "TG_TOKEN": "123456:TESTTOKEN",
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run([sys.executable, "-m", "alembic", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def _catchup_parent() -> str:
    """Ревизия, стоявшая перед догоняющей миграцией."""
    src = (ROOT / "alembic" / "versions" /
           "c41d0f2a7b6e_catch_up_schema.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("down_revision"):
            return line.split("=")[1].strip().strip("'\"")
    raise AssertionError("не найден down_revision догоняющей миграции")


def test_migrations_produce_full_schema():
    """upgrade head на пустой базе → модели и схема совпадают."""
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "drift.db").replace("\\", "/")
        up = _alembic(["upgrade", "head"], db)
        assert up.returncode == 0, up.stderr[-3000:]

        chk = _alembic(["check"], db)
        assert chk.returncode == 0, (
            "Схема из миграций не совпадает с моделями — нужна новая "
            "миграция:\n" + (chk.stdout + chk.stderr)[-3000:])


def test_migrations_are_idempotent_over_create_all_schema():
    """Прод-сценарий: база создана create_all и помечена предыдущей
    ревизией. Догоняющая миграция обязана пропустить уже существующее,
    а не упасть с DuplicateColumn (так мы однажды уронили Railway)."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models.entities import Base

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "prodlike.db").replace("\\", "/")

        async def build():
            engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await engine.dispose()

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(build())

        # помечаем ревизией, что была перед догоняющей — как было в проде
        down = _alembic(["stamp", _catchup_parent()], db)
        assert down.returncode == 0, down.stderr[-2000:]

        up = _alembic(["upgrade", "head"], db)
        assert up.returncode == 0, (
            "Догоняющая миграция упала на схеме от create_all:\n"
            + (up.stdout + up.stderr)[-3000:])
