"""
core/state_utils.py — Central helpers for persistent state files

FIX (#88): Previously every bot had its own state-file logic with slightly
different patterns:
  - Some used direct `open('w').write(json.dumps(...))` → under concurrent
    read a visible half-written file
  - Some had a tmp-file pattern, but without fsync → OS cache could leave
    empty or half-written files behind on a power outage
  - Error handling was inconsistent (some logged, some swallowed)

Now centralised:
  - atomic_write_json: tmp + fsync + os.replace for guaranteed atomicity
  - atomic_read_json: with default fallback on corruption
  - everything with unified logging
"""

import json
import logging
import os
import tempfile
import time
from typing import Any

logger = logging.getLogger(__name__)

# Windows: os.replace fails with PermissionError if a reader holds the
# target file open at exactly the moment of the replace. A short retry
# bridges this narrow window instead of silently discarding the update (P2.49).
_REPLACE_RETRIES = 5
_REPLACE_RETRY_SLEEP_S = 0.05


def _cleanup_tmp(tmp: str) -> None:
    """Removes a leftover temp file without raising itself."""
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass


def atomic_write_json(filepath: str, data: Any, indent: int = 2) -> bool:
    """Writes JSON atomically via temp file + os.replace.

    Returns True on success, False on error (with a log entry).
    A concurrent reader ALWAYS sees either the old or the new
    version, never a half-written intermediate state.

    P2.49 hardening:
      - Unique temp name via ``tempfile.mkstemp`` in the TARGET DIRECTORY instead
        of a fixed ``.tmp``. Two parallel writers on the same path would otherwise
        collide on the same temp file and corrupt each other; the same
        directory keeps ``os.replace`` on one filesystem, so the atomicity
        guarantee is preserved (pattern from core/coins.py, #68).
      - Short retry on ``os.replace``, which on Windows fails with
        ``PermissionError`` as long as a reader holds the target file open. If it
        is still blocked after all attempts, this is LOGGED (no more silent
        update loss) and the temp file is cleaned up.
    """
    if not filepath:
        logger.error("atomic_write_json: empty path passed")
        return False

    # abspath() → dirname() is always non-empty, even for a bare filename.
    parent = os.path.dirname(os.path.abspath(filepath))
    try:
        if not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
    except OSError as e:
        logger.error(f"atomic_write_json: could not create target directory for {filepath}: {e}")
        return False

    basename = os.path.basename(filepath)
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{basename}.", suffix=".tmp")
    except OSError as e:
        logger.error(f"atomic_write_json: could not create temp file for {filepath}: {e}")
        return False

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())

        # os.replace is atomic on POSIX and Windows. On Windows, however, it can
        # fail with PermissionError as long as a reader holds the target file
        # open → short retry instead of silent update loss.
        last_err: OSError | None = None
        for _ in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, filepath)
                return True
            except PermissionError as e:
                last_err = e
                time.sleep(_REPLACE_RETRY_SLEEP_S)
        logger.error(
            f"atomic_write_json: os.replace on {filepath} still blocked after "
            f"{_REPLACE_RETRIES} attempts (reader holding the file open?): {last_err} — update NOT written."
        )
        _cleanup_tmp(tmp)
        return False
    except Exception as e:
        logger.error(f"Error during atomic write of {filepath}: {e}")
        _cleanup_tmp(tmp)
        return False


def atomic_read_json(filepath: str, default: Any = None) -> Any:
    """Reads JSON defensively.

    Returns `default` if:
      - the file does not exist
      - the file is empty
      - a JSON decode error occurs (e.g. corrupted by a previous crash)

    The default is both returned and automatically written as a fresh new
    state file, so bots don't keep running into the corrupt file on every
    start.
    """
    if not filepath or not os.path.exists(filepath):
        return default

    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return default
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error(f"Corrupt state file {filepath}: {e} — backed up as .corrupt, using default")
        # Backup the corrupt file and return default
        try:
            os.replace(filepath, filepath + ".corrupt")
        except Exception:
            pass
        return default
    except Exception as e:
        logger.error(f"Error reading {filepath}: {e}")
        return default
