from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import ConfigManager
from .paths import log_path


def configure_logging(config: ConfigManager) -> logging.Logger:
    settings = config.get("logging", {})
    logger = logging.getLogger("agentlight")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    handler = RotatingFileHandler(
        log_path(),
        maxBytes=int(settings.get("max_bytes", 2_000_000)),
        backupCount=int(settings.get("backup_count", 3)),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger

