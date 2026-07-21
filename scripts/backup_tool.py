"""
Работа с зашифрованной резервной копией: проверка и расшифровка.

Копия уходит в Telegram зашифрованной (`app/services/backup.py`), поэтому
без этого инструмента её не восстановить. Ключ берётся из BACKUP_ENC_KEY —
он должен храниться ОТДЕЛЬНО от самих копий.

    # убедиться, что файл читается тем ключом, что есть под рукой
    python -m scripts.backup_tool --verify backup_2026-07-22.sql.gz.enc

    # получить обычный .gz для восстановления
    python -m scripts.backup_tool --decrypt backup_...enc --out dump.sql.gz

Инструмент НИКОГДА не пишет в рабочую базу: расшифрованный файл кладётся
туда, куда указали, а восстановление выполняется отдельно (psql/gunzip),
осознанно и с полным контролем оператора.
"""
from __future__ import annotations

import argparse
import gzip
import pathlib
import sys

from app.services import backup


def _read(path: str) -> bytes:
    return pathlib.Path(path).read_bytes()


def _describe(raw: bytes) -> str:
    """Что внутри распакованного дампа — без вывода самих данных."""
    if raw.startswith(b"SQLite format 3"):
        return f"база SQLite, {len(raw)} байт"
    tables = raw.count(b"CREATE TABLE")
    return f"SQL-дамп PostgreSQL, {len(raw)} байт, таблиц в схеме: {tables}"


def cmd_verify(path: str) -> int:
    blob = _read(path)
    print(f"Файл: {path} ({len(blob)} байт)")
    print(f"SHA-256: {backup.checksum(blob)}")

    if not blob.startswith(backup.ENC_MAGIC):
        print("Копия НЕ зашифрована (нет метки). Проверяем как обычный архив.")
        problem = backup.verify_dump(blob)
        print(f"Содержимое: {problem or 'похоже на настоящий дамп'}")
        return 1 if problem else 0

    try:
        data = backup.decrypt_backup(blob)
    except (ValueError, RuntimeError) as e:
        print(f"НЕ РАСШИФРОВАНА: {e}")
        return 2
    try:
        raw = gzip.decompress(data)
    except OSError as e:
        print(f"Расшифрована, но не распаковывается: {e}")
        return 2
    print(f"Расшифрована и распакована: {_describe(raw)}")
    return 0


def cmd_decrypt(path: str, out: str, force: bool) -> int:
    dest = pathlib.Path(out)
    if dest.exists() and not force:
        print(f"Файл {out} уже существует. Укажите другой путь или --force.")
        return 2
    try:
        data = backup.decrypt_backup(_read(path))
    except (ValueError, RuntimeError) as e:
        print(f"НЕ РАСШИФРОВАНА: {e}")
        return 2
    dest.write_bytes(data)
    print(f"Готово: {out} ({len(data)} байт). "
          "Восстановление выполняйте отдельно, см. DISASTER_RECOVERY.md.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--verify", metavar="FILE",
                       help="проверить, что копия читается и содержит базу")
    group.add_argument("--decrypt", metavar="FILE",
                       help="расшифровать копию в обычный .gz")
    parser.add_argument("--out", help="куда положить расшифрованный файл")
    parser.add_argument("--force", action="store_true",
                        help="перезаписать существующий файл назначения")
    args = parser.parse_args()

    if args.verify:
        return cmd_verify(args.verify)
    if not args.out:
        print("Для --decrypt нужен --out")
        return 2
    return cmd_decrypt(args.decrypt, args.out, args.force)


if __name__ == "__main__":
    sys.exit(main())
