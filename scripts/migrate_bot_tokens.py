"""
Перенос токенов Telegram/VK клубов в зашифрованные колонки.

Порядок безопасного перехода (подробно — в DISASTER_RECOVERY.md):

    1) деплой кода, читающего оба формата (уже в main);
    2) задать BOT_TOKEN_ENC_KEY;
    3) `--dry-run`  — сколько клубов затронет, без изменений;
    4) снять бэкап;
    5) `--apply`    — шифрует и ОЧИЩАЕТ открытые колонки (идемпотентно);
    6) `--verify`   — не осталось ли plaintext и всё ли расшифровывается;
    7) перевыпустить токены у @BotFather и в VK: старые значения успели
       побывать в дампах, а дампы уходили в Telegram.

Команда никогда не печатает сами токены — только количества и id клубов.

Использование:
    python -m scripts.migrate_bot_tokens --dry-run
    python -m scripts.migrate_bot_tokens --apply
    python -m scripts.migrate_bot_tokens --verify
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core import bot_tokens
from app.db.engine import SessionLocal
from app.models.entities import Tenant

KINDS = ("tg", "vk")


async def _scan(session, apply: bool) -> tuple[int, int, list[int]]:
    """(перенесено, уже зашифровано, id с нечитаемым шифротекстом)."""
    moved = already = 0
    broken: list[int] = []
    for t in (await session.execute(select(Tenant))).scalars().all():
        for kind in KINDS:
            plain = (getattr(t, f"{kind}_token", "") or "").strip()
            enc = getattr(t, f"{kind}_token_enc", "") or ""
            if enc:
                if not bot_tokens.decrypt(
                        enc, getattr(t, f"{kind}_token_ver", "")):
                    broken.append(t.id)
                else:
                    already += 1
                continue
            if not plain:
                continue
            moved += 1
            if apply:
                # set_token сам шифрует и очищает открытую колонку
                bot_tokens.set_token(t, kind, plain)
    return moved, already, sorted(set(broken))


async def _verify(session) -> int:
    plaintext_left: list[int] = []
    unreadable: list[int] = []
    total = 0
    for t in (await session.execute(select(Tenant))).scalars().all():
        for kind in KINDS:
            if (getattr(t, f"{kind}_token", "") or "").strip():
                plaintext_left.append(t.id)
            enc = getattr(t, f"{kind}_token_enc", "") or ""
            if enc:
                total += 1
                if not bot_tokens.decrypt(
                        enc, getattr(t, f"{kind}_token_ver", "")):
                    unreadable.append(t.id)
    print(f"Проверка: зашифрованных токенов {total}, "
          f"осталось открытым текстом {len(plaintext_left)}, "
          f"не расшифровано {len(unreadable)}")
    if plaintext_left:
        print(f"  открытым текстом у клубов: {sorted(set(plaintext_left))[:20]}")
    if unreadable:
        print(f"  не расшифровано у клубов: {sorted(set(unreadable))[:20]}")
    return 1 if (plaintext_left or unreadable) else 0


async def main_async(mode: str) -> int:
    if not bot_tokens.key_configured():
        print("BOT_TOKEN_ENC_KEY не задан — шифровать нечем. "
              "Задайте ключ и повторите.")
        return 2

    async with SessionLocal() as session:
        if mode == "verify":
            return await _verify(session)

        moved, already, broken = await _scan(session, apply=(mode == "apply"))
        print(f"Режим: {mode}")
        print(f"  уже зашифровано:        {already}")
        print(f"  будет перенесено:       {moved}"
              if mode == "dry-run" else f"  перенесено:             {moved}")
        if broken:
            print(f"  НЕ РАСШИФРОВАНЫ у клубов: {broken}")
            print("    Нет подходящего ключа. Добавьте прежний в "
                  "BOT_TOKEN_KEYRING и повторите — иначе боты этих клубов "
                  "не поднимутся.")

        if mode == "apply":
            if broken:
                await session.rollback()
                print("\nПРИМЕНЕНИЕ ОТМЕНЕНО — см. отчёт выше. Изменений нет.")
                return 2
            await session.commit()
            print("\nГотово. Открытые значения очищены. "
                  "Перевыпустите токены у @BotFather и в VK: прежние успели "
                  "побывать в резервных копиях.")
        else:
            await session.rollback()
            print("\nЭто был пробный прогон, база не изменена.")
        return 2 if broken else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    group.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    mode = "apply" if args.apply else "verify" if args.verify else "dry-run"
    return asyncio.run(main_async(mode))


if __name__ == "__main__":
    sys.exit(main())
