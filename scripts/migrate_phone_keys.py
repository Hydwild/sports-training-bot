"""
Перевод телефонов веб-клиентов на активный ключ (см. app/core/phones.py).

Зачем отдельная команда, а не миграция Alembic: пересчёт требует секретов
из окружения и должен выполняться осознанно, отдельно от деплоя кода.
Порядок безопасного перехода описан в DISASTER_RECOVERY.md:

    1) деплой кода, который читает и старый, и новый формат;
    2) добавление PHONE_ENC_KEY (и PHONE_KEYRING со старым ключом);
    3) `--dry-run`  — что будет сделано, без изменений;
    4) `--apply`    — пересчёт (идемпотентно, можно повторять);
    5) `--verify`   — все ли строки на активной версии и читаются;
    6) только потом — удаление legacy-ключа и ротация JWT_SECRET.

Команда НИКОГДА не печатает телефоны и ключи: только количества и id.

Использование:
    python -m scripts.migrate_phone_keys --dry-run
    python -m scripts.migrate_phone_keys --apply
    python -m scripts.migrate_phone_keys --verify
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from sqlalchemy import select

from app.core import phones
from app.db.engine import SessionLocal
from app.models.entities import WebCustomer


class Report:
    def __init__(self) -> None:
        self.total = 0
        self.already_active = 0
        self.to_reencrypt = 0
        self.to_reindex = 0
        self.unreadable: list[int] = []      # id, телефон не расшифровался
        self.collisions: list[tuple[int, ...]] = []   # id, дающие один индекс

    @property
    def blocked(self) -> bool:
        return bool(self.unreadable or self.collisions)

    def print(self, mode: str) -> None:
        print(f"Режим: {mode}")
        print(f"  всего клиентов:            {self.total}")
        print(f"  уже на активном ключе:     {self.already_active}")
        print(f"  требуют перешифрования:    {self.to_reencrypt}")
        print(f"  требуют переиндексации:    {self.to_reindex}")
        if self.unreadable:
            print(f"  НЕ РАСШИФРОВАНЫ ({len(self.unreadable)}): "
                  f"id={self.unreadable[:20]}"
                  f"{' …' if len(self.unreadable) > 20 else ''}")
            print("    Нет ключа их версии. Добавьте его в PHONE_KEYRING "
                  "и повторите — иначе номера будут потеряны.")
        if self.collisions:
            print(f"  ДУБЛИ ({len(self.collisions)}): один и тот же номер у "
                  f"разных клиентов")
            for ids in self.collisions[:20]:
                print(f"    id={list(ids)}")
            print("    Слияние не выполняется автоматически: у клиентов "
                  "разные записи, оценки и ссылки управления. Объедините "
                  "их вручную или удалите лишнего через панель оператора.")


async def _scan(session, apply: bool) -> Report:
    rep = Report()
    active = phones.active_key_ver()
    rows = (await session.execute(select(WebCustomer))).scalars().all()
    rep.total = len(rows)

    # что получится после пересчёта: ищем столкновения ДО изменений
    planned: dict[tuple[int, str], list[int]] = defaultdict(list)

    for row in rows:
        phone = phones.decrypt(row.phone_enc, row.key_ver)
        if not phone:
            rep.unreadable.append(row.id)
            continue

        need_enc = row.key_ver != active
        need_idx = getattr(row, "index_ver", row.key_ver) != active
        if not (need_enc or need_idx):
            rep.already_active += 1
            planned[(row.tenant_id, row.phone_index)].append(row.id)
            continue

        new_index = phones.phone_index(phone, active)
        planned[(row.tenant_id, new_index)].append(row.id)

        if need_enc:
            rep.to_reencrypt += 1
        if need_idx:
            rep.to_reindex += 1

        if apply:
            if need_enc:
                row.phone_enc, row.key_ver = phones.encrypt(phone, active)
            row.phone_index = new_index
            row.index_ver = active

    rep.collisions = [tuple(ids) for ids in planned.values() if len(ids) > 1]
    return rep


async def _verify(session) -> int:
    """0 — всё на активной версии и читается."""
    active = phones.active_key_ver()
    rows = (await session.execute(select(WebCustomer))).scalars().all()
    stale = [r.id for r in rows
             if r.key_ver != active
             or getattr(r, "index_ver", r.key_ver) != active]
    unreadable = [r.id for r in rows
                  if not phones.decrypt(r.phone_enc, r.key_ver)]
    print(f"Проверка: всего {len(rows)}, на активном ключе "
          f"{len(rows) - len(stale)}, отстали {len(stale)}, "
          f"не расшифрованы {len(unreadable)}")
    if stale:
        print(f"  отстали: id={stale[:20]}{' …' if len(stale) > 20 else ''}")
    if unreadable:
        print(f"  не расшифрованы: id={unreadable[:20]}")
    return 1 if (stale or unreadable) else 0


async def main_async(mode: str) -> int:
    print(f"Активная версия ключа телефонов: {phones.active_key_ver()}")
    print(f"Доступные версии для чтения: {', '.join(phones.known_key_versions())}")

    async with SessionLocal() as session:
        if mode == "verify":
            return await _verify(session)

        rep = await _scan(session, apply=(mode == "apply"))
        rep.print(mode)

        if mode == "apply":
            if rep.blocked:
                # применять нечего: сначала нужно разобраться с дублями и
                # нечитаемыми строками, иначе часть клиентов потеряется
                await session.rollback()
                print("\nПРИМЕНЕНИЕ ОТМЕНЕНО — см. отчёт выше. Изменений нет.")
                return 2
            await session.commit()
            print("\nГотово. Повторный запуск ничего не изменит.")
        else:
            await session.rollback()
            print("\nЭто был пробный прогон, база не изменена.")
        return 2 if rep.blocked else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="показать план, ничего не менять")
    group.add_argument("--apply", action="store_true",
                       help="пересчитать (идемпотентно)")
    group.add_argument("--verify", action="store_true",
                       help="проверить, что всё на активном ключе")
    args = parser.parse_args()
    mode = "apply" if args.apply else "verify" if args.verify else "dry-run"
    return asyncio.run(main_async(mode))


if __name__ == "__main__":
    sys.exit(main())
