"""Logging to the container log and to /config/logs/narrator.log."""

import logging
import logging.handlers
import os
from pathlib import Path

log = logging.getLogger("narrator")


def setup():
    if log.handlers:
        return
    log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    # Libraries add their own root handler; without this every line is logged twice
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    logs = Path(os.environ.get("CONFIG_DIR", "/config")) / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        f = logging.handlers.RotatingFileHandler(logs / "narrator.log", maxBytes=2_000_000, backupCount=3)
        f.setFormatter(fmt)
        log.addHandler(f)
    except OSError:
        pass
