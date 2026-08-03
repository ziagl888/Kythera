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
    Sets up logging for a bot/process.

    Writes simultaneously to:
      - stdout  (readable by the watchdog/systemd)
      - logs/<name>.log  (persistent file, max ~10 MB, then rotated)

    Usage in every bot — replaces the local basicConfig() calls:

        from core.logging_setup import setup_logging
        logger = setup_logging("AI_MIS_BOT")

    Args:
        name:    Process name — appears in the log format and as the filename.
        level:   Log level (default: INFO).
        log_dir: Directory for log files (created automatically).
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
