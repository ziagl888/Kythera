# core/logging_setup.py
# Central logging for all processes
import logging
import sys
from pathlib import Path


def setup_logging(
    name: str,
    level: int = logging.INFO,
    log_dir: str = "logs",
) -> logging.Logger:
    """
    Richtet Logging für einen Bot/Prozess ein.

    Schreibt gleichzeitig in:
      - stdout  (für den Watchdog/systemd lesbar)
      - logs/<name>.log  (persistente Datei, max ~10 MB, dann rotiert)

    Verwendung in jedem Bot — ersetzt die lokalen basicConfig()-Aufrufe:

        from core.logging_setup import setup_logging
        logger = setup_logging("AI_MIS_BOT")

    Args:
        name:    Prozessname — erscheint im Log-Format und als Dateiname.
        level:   Log-Level (Standard: INFO).
        log_dir: Verzeichnis für Log-Dateien (wird automatisch angelegt).
    """
    Path(log_dir).mkdir(exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid duplicate handlers (e.g. on module reloads)
    if logger.handlers:
        return logger

    # --- stdout ---
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # --- Rotating file handler (10 MB, 3 backups) ---
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(
        filename=f"{log_dir}/{name}.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Don't flood the root logger
    logger.propagate = False

    return logger
