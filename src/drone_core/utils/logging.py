"""Настройка логирования: вывод в stdout (→ journald под systemd) И в
ротируемый файл в каталоге логов — чтобы после сессии подключения дрона можно
было спокойно разобрать «что пошло не так», не копаясь в journalctl.

Каталог логов: env `SKYBITE_LOG_DIR`, иначе `<repo>/logs`
(на сервере WorkingDirectory=/opt/skybite → /opt/skybite/logs).

Каждый процесс пишет в СВОЙ файл (`name`), чтобы несколько мостов не дрались
за один файл при ротации:
  - веб-приложение   → web.log       (включает radio-реле, preflight, команды)
  - мост дрона       → bridge-<id>.log (connect/heartbeat/health/arm/test_hop)
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _default_log_dir() -> Path:
    env = os.environ.get("SKYBITE_LOG_DIR")
    if env:
        return Path(env)
    # <repo>/logs: utils(0)/drone_core(1)/src(2)/<repo>(3)
    return Path(__file__).resolve().parents[3] / "logs"


def setup(level: str = "INFO", name: str = "skybite") -> None:
    fmt = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # Файловый лог — best-effort: если каталог недоступен, не роняем процесс,
    # просто продолжаем со stdout (journald всё равно всё соберёт).
    try:
        log_dir = _default_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / f"{name}.log",
            maxBytes=5_000_000, backupCount=5, encoding="utf-8",
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
        root.info("📝 file logging → %s", log_dir / f"{name}.log")
    except Exception as e:  # pragma: no cover — диагностика, не критично
        root.warning("file logging disabled (%r) — only stdout/journald", e)

    root.setLevel(level.upper())
