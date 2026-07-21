"""
Обёртка над alembic для start.sh: если БД уже содержит схему, но не
застолблена alembic'ом (app/db/pre_migrate.py::needs_stamp), делает
"alembic stamp head" вместо "alembic upgrade head" — иначе обычный upgrade
пытается заново создать существующие таблицы и падает DuplicateTableError.

needs_stamp разрешает stamp только при явном флаге ALLOW_LEGACY_STAMP=1 и
глубоком совпадении схемы с моделями. Без флага (обычный деплой) он всегда
False, и здесь выполняется обычный upgrade head: если он упадёт — деплой
честно остановится, ничего молча не «чинится».

Любая другая ошибка миграции по-прежнему завершает процесс с ненулевым
кодом (start.sh останавливает деплой).
"""
import asyncio
import subprocess
import sys

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.pre_migrate import needs_stamp


def main() -> int:
    engine = create_async_engine(settings.database_url)
    try:
        stamp_needed = asyncio.run(needs_stamp(engine))
    finally:
        asyncio.run(engine.dispose())

    if stamp_needed:
        print("PRE-MIGRATE: схема уже существует, но alembic_version не "
              "заведён — помечаем текущую ревизию применённой (stamp) "
              "вместо повторного создания таблиц")
        return subprocess.call(["alembic", "stamp", "head"])
    return subprocess.call(["alembic", "upgrade", "head"])


if __name__ == "__main__":
    sys.exit(main())
