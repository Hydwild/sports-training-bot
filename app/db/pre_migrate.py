"""
Проверка перед `alembic upgrade head` для одного переходного сценария.

Исторически таблицы Postgres создавались автостартовым create_all ещё до
того, как в проект завели Alembic: на проде схема существует, а
alembic_version пуст. Обычный upgrade head в этом случае пытается заново
создать таблицы и падает DuplicateTableError.

`alembic stamp head` помечает миграции применёнными без выполнения DDL —
но пометить неполную или несовместимую схему значит запустить приложение
против базы, где нет нужной колонки, стоит другой тип или отсутствует
внешний ключ. Расхождение всплывёт позже и в неожиданном месте.

Поэтому stamp разрешён только при двух условиях СРАЗУ:

  1) оператор явно разрешил его одноразовым флагом ALLOW_LEGACY_STAMP=1
     (в обычном деплое флага нет — упавший upgrade честно останавливает
     выкат, а не «чинится» молча);

  2) фактическая схема ГЛУБОКО совпадает с моделями: таблицы, колонки,
     типы, nullable, primary key, внешние ключи, уникальные ограничения и
     индексы. Любое неоднозначное расхождение → отказ с отчётом, ничего
     не штампуется.

Чистая установка сюда не попадает: там нет таблицы tenants, функция
возвращает False, и обычный upgrade head создаёт схему с нуля.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.entities import Base

logger = logging.getLogger("app")

ALLOW_FLAG = "ALLOW_LEGACY_STAMP"


def stamp_allowed_by_operator() -> bool:
    return os.environ.get(ALLOW_FLAG, "").strip() in ("1", "true", "yes")


def _norm_type(sqltype) -> str:
    """Грубая нормализация типа для сравнения между моделью и базой.
    Точное совпадение диалектных типов недостижимо (VARCHAR(200) в модели
    против character varying в интроспекции), поэтому сводим к семье:
    int / str / bool / datetime / other."""
    s = str(sqltype).lower()
    if any(k in s for k in ("int", "serial", "bigint")):
        return "int"
    if any(k in s for k in ("char", "text", "clob", "string")):
        return "str"
    if "bool" in s:
        return "bool"
    if any(k in s for k in ("date", "time")):
        return "datetime"
    if any(k in s for k in ("float", "real", "double", "numeric", "decimal")):
        return "num"
    return "other"


def schema_diffs(sync_conn) -> list[str]:
    """Все значимые расхождения фактической схемы с моделями.
    Пустой список — можно штамповать."""
    insp = inspect(sync_conn)
    existing = set(insp.get_table_names())
    diffs: list[str] = []

    for name, table in Base.metadata.tables.items():
        if name not in existing:
            diffs.append(f"нет таблицы {name}")
            continue

        db_cols = {c["name"]: c for c in insp.get_columns(name)}
        for col in table.columns:
            db = db_cols.get(col.name)
            if db is None:
                diffs.append(f"{name}: нет колонки {col.name}")
                continue
            want, got = _norm_type(col.type), _norm_type(db["type"])
            if want != got:
                diffs.append(
                    f"{name}.{col.name}: тип {got} вместо {want}")
            # nullable сверяем только когда модель ЖЁСТКО требует NOT NULL:
            # обратная сторона (в базе строже) приложению не мешает
            if not col.nullable and db.get("nullable", True):
                diffs.append(f"{name}.{col.name}: допускает NULL, модель — нет")

        # первичный ключ
        want_pk = {c.name for c in table.primary_key.columns}
        got_pk = set(insp.get_pk_constraint(name).get(
            "constrained_columns") or [])
        if want_pk != got_pk:
            diffs.append(f"{name}: первичный ключ {got_pk or '—'} "
                         f"вместо {want_pk}")

        # внешние ключи — сверяем по набору колонок-источников
        want_fk = {tuple(sorted(fk.column_keys))
                   for fk in table.constraints
                   if fk.__class__.__name__ == "ForeignKeyConstraint"}
        got_fk = {tuple(sorted(fk["constrained_columns"]))
                  for fk in insp.get_foreign_keys(name)}
        for miss in want_fk - got_fk:
            diffs.append(f"{name}: нет внешнего ключа по {list(miss)}")

        # уникальные ограничения (в т.ч. поднятые как unique-индексы)
        want_uq = {tuple(sorted(c.name for c in uc.columns))
                   for uc in table.constraints
                   if uc.__class__.__name__ == "UniqueConstraint"}
        got_uq = {tuple(sorted(u["column_names"]))
                  for u in insp.get_unique_constraints(name)}
        got_uq |= {tuple(sorted(i["column_names"]))
                   for i in insp.get_indexes(name) if i.get("unique")}
        for miss in want_uq - got_uq:
            diffs.append(f"{name}: нет уникального ограничения по {list(miss)}")

        # индексы модели (кроме тех, что уже покрыты unique/pk)
        want_idx = {tuple(sorted(c.name for c in ix.columns))
                    for ix in table.indexes}
        got_idx = {tuple(sorted(i["column_names"]))
                   for i in insp.get_indexes(name)}
        for miss in want_idx - got_idx - want_uq:
            diffs.append(f"{name}: нет индекса по {list(miss)}")

    return diffs


# сохранён под прежним именем: им пользуются тесты и другой код
def schema_gaps(sync_conn) -> list[str]:
    return schema_diffs(sync_conn)


def _check_sync(sync_conn) -> bool:
    insp = inspect(sync_conn)
    tables = insp.get_table_names()
    if "tenants" not in tables:
        return False  # чистая база — обычный upgrade head создаст всё сам
    if "alembic_version" in tables:
        row = sync_conn.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        if row is not None:
            return False        # alembic уже ведёт эту базу

    if not stamp_allowed_by_operator():
        logger.error(
            "PRE-MIGRATE: схема без alembic_version, но %s не задан. "
            "Автоматический stamp запрещён: разберитесь со схемой и "
            "запустите один раз с %s=1, либо приведите базу под обычный "
            "upgrade head.", ALLOW_FLAG, ALLOW_FLAG)
        return False

    diffs = schema_diffs(sync_conn)
    if diffs:
        logger.error(
            "PRE-MIGRATE: stamp запрещён — схема расходится с моделями "
            "(%d, например: %s). Штамповать её нельзя.",
            len(diffs), "; ".join(diffs[:8]))
        return False

    logger.warning(
        "PRE-MIGRATE: схема глубоко совпадает с моделями и оператор разрешил "
        "stamp флагом %s. Помечаем миграции применёнными. СНИМИТЕ флаг после "
        "успешного деплоя.", ALLOW_FLAG)
    return True


async def needs_stamp(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        return await conn.run_sync(_check_sync)
