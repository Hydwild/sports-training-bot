"""
Конфигуратор бота для клиента: собирает готовую папку (код + .env + при
указанном ID тренера — seed-базу с уже настроенным клубом) в zip-архив.

Используется из двух мест с разными правами доступа:
  - app/admin/routes.py (владелец конкретного клуба, /admin/builder),
  - app/admin/platform.py (оператор площадки, /admin/platform/builder).
Логика сборки общая — вынесена сюда, чтобы не дублировать ~150 строк.
"""
from __future__ import annotations

import io
import secrets
import zipfile
from pathlib import Path


def _int_or_none(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.isdigit() else None


async def build_bot_bundle(
    *, club_name: str, edition: str = "lite", timezone: str = "Europe/Moscow",
    tg_token: str, vk_token: str = "", admin_tg_id: str = "",
    admin_vk_id: str = "", brand_name: str = "", brand_color: str = "#3a7bd5",
    reminder_enabled: str = "", reminder_minutes: str = "60",
    cancel_lock_minutes: str = "0", signup_close_minutes: str = "0",
    welcome_text: str = "", tg_bot_username: str = "", public_base_url: str = "",
    yookassa_shop_id: str = "", yookassa_secret_key: str = "",
    vertical: str = "sport",
) -> tuple[bytes, str]:
    """Возвращает (zip_bytes, filename) готовой сборки бота под клиента."""
    root = Path(__file__).resolve().parents[2]
    from app.core.verticals import VERTICALS
    edition = "pro" if edition == "pro" else "lite"
    vertical = vertical if vertical in VERTICALS else "sport"
    vk_token = vk_token.strip()
    tz = timezone.strip() or "Europe/Moscow"
    name = club_name.strip()[:100]

    admin_tg = _int_or_none(admin_tg_id)
    admin_vk = _int_or_none(admin_vk_id)
    rem_minutes = _int_or_none(reminder_minutes) or 60
    lock_minutes = _int_or_none(cancel_lock_minutes) or 0
    close_minutes = _int_or_none(signup_close_minutes) or 0

    env_lines = [
        f"# Бот для клуба: {name}",
        f"EDITION={edition}",
        "DATABASE_URL=sqlite+aiosqlite:////data/badminton.db",
        f"TG_TOKEN={tg_token.strip()}",
        "TG_MODE=polling",
        f"VK_TOKEN={vk_token}",
        f"RUN_VK_POLLING={'true' if vk_token else 'false'}",
        f"JWT_SECRET={secrets.token_urlsafe(24)}",
        f"ADMIN_API_TOKEN={secrets.token_urlsafe(24)}",
        f"TIMEZONE={tz}",
        "LOG_DIR=/data/logs",
        "PORT=8080",
    ]
    if edition == "pro":
        if tg_bot_username.strip():
            env_lines.append(f"TG_BOT_USERNAME={tg_bot_username.strip()}")
        if public_base_url.strip():
            env_lines.append(f"PUBLIC_BASE_URL={public_base_url.strip()}")
        if yookassa_shop_id.strip():
            env_lines.append(f"YOOKASSA_SHOP_ID={yookassa_shop_id.strip()}")
        if yookassa_secret_key.strip():
            env_lines.append(f"YOOKASSA_SECRET_KEY={yookassa_secret_key.strip()}")
    env_text = "\n".join(env_lines) + "\n"

    # ─── seed-база: клуб и тренер настроены заранее ───
    seed_bytes = None
    if admin_tg or admin_vk:
        import tempfile
        import os as _os
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.models.entities import Base, Tenant

        _fd, tmp_path = tempfile.mkstemp(suffix=".db")
        _os.close(_fd)
        tmp_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}")
        try:
            async with tmp_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            TmpSession = async_sessionmaker(tmp_engine, expire_on_commit=False)
            async with TmpSession() as tmp_session:
                tenant = Tenant(
                    name=name, timezone=tz, vertical=vertical,
                    admin_tg_id=admin_tg, admin_vk_id=admin_vk,
                    brand_name=brand_name.strip()[:200] or name,
                    brand_color=brand_color.strip() or "#3a7bd5",
                    reminder_enabled=bool(reminder_enabled),
                    reminder_minutes=rem_minutes,
                    cancel_lock_minutes=lock_minutes,
                    signup_close_minutes=close_minutes,
                    welcome_text=welcome_text.strip()[:1000] or None,
                )
                tmp_session.add(tenant)
                await tmp_session.commit()
            await tmp_engine.dispose()
            with open(tmp_path, "rb") as f:
                seed_bytes = f.read()
        finally:
            if _os.path.exists(tmp_path):
                _os.remove(tmp_path)

    onboarding = (
        "5. Тренер сразу увидит меню управления — клуб и права уже настроены\n"
        if seed_bytes else
        "5. Клуб не привязан к тренеру автоматически (ID не был указан) — "
        "создайте клуб и роль через Swagger (/docs), как описано в "
        "DEPLOY_CLIENT.md\n"
    )
    setup_md = (
        f"# Бот для клуба «{name}»\n\n"
        "Готовая сборка. Развёртывание на Railway:\n\n"
        "1. Залейте эту папку в новый GitHub-репозиторий\n"
        "2. Railway → New Project → Deploy from GitHub repo\n"
        "3. Settings → Volumes → Add Volume, mount path: /data\n"
        "4. Variables → Raw Editor → вставьте содержимое файла .env\n"
        f"{onboarding}"
        "6. Напишите боту /start в Telegram (и сообществу в ВК, если указано)\n\n"
        "Подробная инструкция — в DEPLOY_CLIENT.md\n"
    )

    include_files = ["Dockerfile", "start.sh", "requirements.txt",
                     "README.md", "DEPLOY_CLIENT.md", "alembic.ini"]
    include_dirs = ["app", "alembic", "migrations", "tests"]
    skip = ("__pycache__", ".pytest_cache", "logs")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname_ in include_files:
            p = root / fname_
            if p.is_file():
                zf.write(p, fname_)
        for d in include_dirs:
            base = root / d
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(root)
                if any(part in skip for part in rel.parts):
                    continue
                if p.suffix in (".db", ".pyc") or p.name == ".env":
                    continue
                zf.write(p, str(rel))
        zf.writestr(".env", env_text)
        zf.writestr("SETUP.md", setup_md)
        if seed_bytes:
            zf.writestr("seed.db", seed_bytes)
    buf.seek(0)

    safe = "".join(c for c in club_name
                   if c.isascii() and (c.isalnum() or c in "-_ "))
    out_name = f"bot_{safe.strip().replace(' ', '_') or 'client'}.zip"
    return buf.getvalue(), out_name
