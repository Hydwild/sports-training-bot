"""Логирование: консоль + bot.log (всё) + errors.log (только ошибки), с ротацией."""
import logging
import logging.handlers
import sys
from pathlib import Path

from app.core.config import settings

_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging() -> Path:
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = logging.Formatter(_FMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    full = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    full.setFormatter(fmt)
    root.addHandler(full)

    errors = logging.handlers.RotatingFileHandler(
        log_dir / "errors.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    errors.setLevel(logging.ERROR)
    errors.setFormatter(fmt)
    root.addHandler(errors)

    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("uncaught").critical(
            "Необработанное исключение", exc_info=(exc_type, exc_value, exc_tb))
    sys.excepthook = _hook
    return log_dir
