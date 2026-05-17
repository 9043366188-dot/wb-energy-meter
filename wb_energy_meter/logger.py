"""Настройка логирования."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys


LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    numeric = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(numeric)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding="utf-8",
            )
            fh.setLevel(numeric)
            fh.setFormatter(formatter)
            root.addHandler(fh)
        except OSError as e:
            root.error("Не удалось открыть файл лога %s: %s", log_file, e)

    logging.getLogger("paho").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
