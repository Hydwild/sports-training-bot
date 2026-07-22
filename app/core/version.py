"""
Идентификатор развёрнутой сборки — короткий SHA коммита.

Отдаётся в /health, чтобы можно было ПРОВЕРИТЬ, какой именно код сейчас в
проде: без этого «задеплоилось или нет» приходится угадывать. Значение
берётся из окружения (Railway кладёт RAILWAY_GIT_COMMIT_SHA, наш Dockerfile
пробрасывает GIT_SHA как ARG→ENV), с фолбэком на локальный git при запуске
из рабочей копии. Если ничего нет — "unknown", а не падение.
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache


@lru_cache
def commit_sha() -> str:
    for var in ("GIT_SHA", "RAILWAY_GIT_COMMIT_SHA", "SOURCE_COMMIT",
                "COMMIT_SHA"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val[:12]
    # локальный запуск из рабочей копии — спросим git, но тихо
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=3,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"
