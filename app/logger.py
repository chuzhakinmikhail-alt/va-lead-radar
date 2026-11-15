"""Logging setup: rotating file handler plus console output."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_path: Path, level: str = "INFO") -> None:
    """Configure root logging with a rotating file and a stream handler."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(resolved_level)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(resolved_level)
    root.addHandler(stream_handler)

    # aiogram is chatty on INFO for every update; keep it at WARNING.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
