"""
core/state_utils.py — Zentrale Helper für persistente State-Dateien

FIX (#88): Vorher hatte jeder Bot seine eigene State-File-Logik mit leicht
unterschiedlichen Patterns:
  - Some used direct `open('w').write(json.dumps(...))` → under concurrent
    Read sichtbarer halb-geschriebener File
  - Manche hatten tmp-File-Pattern, aber ohne fsync → OS-Cache konnte bei
    Stromausfall leere oder halb-geschriebene Files hinterlassen
  - Error-Handling war inkonsistent (manche loggten, manche schluckten)

Jetzt zentral:
  - atomic_write_json: tmp + fsync + os.replace für garantierte Atomicity
  - atomic_read_json: mit Default-Fallback bei Korruption
  - Alles mit einheitlichem Logging
"""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_json(filepath: str, data: Any, indent: int = 2) -> bool:
    """Schreibt JSON atomar via Temp-File + os.replace.

    Returns True bei Erfolg, False bei Fehler (mit Log-Entry).
    Ein konkurrenter Reader sieht IMMER entweder die alte oder die neue
    Version, niemals einen halb-geschriebenen Zwischenstand.
    """
    if not filepath:
        logger.error("atomic_write_json: Leerer Pfad übergeben")
        return False

    tmp = filepath + ".tmp"
    try:
        # Ensure the target directory exists
        parent = os.path.dirname(os.path.abspath(filepath))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())

        # os.replace is atomic on POSIX and Windows
        os.replace(tmp, filepath)
        return True
    except Exception as e:
        logger.error(f"Error during atomic write von {filepath}: {e}")
        # Temp-File aufräumen falls übrig
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def atomic_read_json(filepath: str, default: Any = None) -> Any:
    """Liest JSON defensiv.

    Returns `default` wenn:
      - File nicht existiert
      - File leer ist
      - JSON-Decode-Fehler (z.B. korrupt durch vorherigen Crash)

    Der Default wird sowohl zurückgegeben als auch automatisch als neue
    frische State-Datei geschrieben, damit Bots nicht bei jedem Start
    immer wieder auf die korrupte Datei stoßen.
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
        logger.error(f"Error reading von {filepath}: {e}")
        return default
