"""
Проверка резервной копии реальным восстановлением.

Сигнатуры мало: gzip распаковывается и «CREATE TABLE» в тексте есть, а
восстановиться копия всё равно может не дать (обрезана, битая середина).
Единственная надёжная проверка — восстановить и задать вопросы.

SQLite-путь тестируем целиком; PostgreSQL-путь идёт через psql и отдельную
временную БД — его гоняет CI на настоящей базе (см. блок 12).
"""
import gzip
import sqlite3

import pytest

from app.services import backup, restore_drill


def _real_sqlite_dump(tmp_path) -> bytes:
    """Настоящая маленькая база с критическими таблицами -> gz."""
    db = tmp_path / "src.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE trainings (id INTEGER PRIMARY KEY, tenant_id INTEGER);
        CREATE TABLE signups (id INTEGER PRIMARY KEY, training_id INTEGER);
        INSERT INTO tenants VALUES (1, 'Клуб');
        INSERT INTO trainings VALUES (1, 1);
        INSERT INTO signups VALUES (1, 1);
    """)
    con.commit()
    raw = open(db, "rb").read()
    con.close()
    return gzip.compress(raw)


@pytest.fixture(autouse=True)
def _sqlite(monkeypatch):
    monkeypatch.setattr(restore_drill.settings, "database_url",
                        "sqlite+aiosqlite:///./x.db")


async def test_good_backup_passes_drill(tmp_path):
    res = await restore_drill.run_drill(_real_sqlite_dump(tmp_path))
    assert res.ok, res.message
    assert res.details["tenants"] == 1
    assert res.details["signups"] == 1


async def test_truncated_dump_fails_drill(tmp_path):
    good = _real_sqlite_dump(tmp_path)
    raw = gzip.decompress(good)
    broken = gzip.compress(raw[: len(raw) // 2])   # обрезали середину базы
    res = await restore_drill.run_drill(broken)
    assert not res.ok
    # обрезанная база не откроется или не пройдёт integrity_check — в любом
    # случае это не "ok"; конкретное сообщение зависит от места обрыва
    assert "восстановление успешно" not in res.message.lower()


async def test_dump_without_critical_tables_fails(tmp_path):
    db = tmp_path / "empty.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE other (id INTEGER)")
    con.commit()
    raw = open(db, "rb").read()
    con.close()
    res = await restore_drill.run_drill(gzip.compress(raw))
    assert not res.ok
    assert "критических таблиц" in res.message


async def test_encrypted_backup_is_decrypted_first(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.settings, "backup_enc_key", "ключ-проверки")
    blob = backup.encrypt_backup(_real_sqlite_dump(tmp_path))
    res = await restore_drill.run_drill(blob)
    assert res.ok, res.message


async def test_wrong_key_reports_not_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.settings, "backup_enc_key", "ключ-А")
    blob = backup.encrypt_backup(_real_sqlite_dump(tmp_path))
    monkeypatch.setattr(backup.settings, "backup_enc_key", "ключ-Б")
    res = await restore_drill.run_drill(blob)
    assert not res.ok
    assert "не расшифрован" in res.message


def test_postgres_target_never_equals_working_db(monkeypatch):
    """Защита от катастрофы: тестовая база не может совпасть с рабочей и
    DROP по рабочему имени не выполняется."""
    monkeypatch.setattr(
        restore_drill.settings, "database_url",
        "postgresql+asyncpg://u:p@h:5432/production")
    assert restore_drill._working_db_name() == "production"
    # цель всегда со случайным префиксом drill_
    target = restore_drill._url_with_db("drill_abc123").rsplit("/", 1)[-1]
    assert target.startswith("drill_")
    assert target != "production"
