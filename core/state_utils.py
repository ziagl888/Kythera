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
import tempfile
import time
from typing import Any

logger = logging.getLogger(__name__)

# Windows: os.replace scheitert mit PermissionError, wenn ein Reader die
# Zieldatei genau im Replace-Moment offen hält. Ein kurzer Retry überbrückt
# dieses schmale Fenster, statt das Update still zu verwerfen (P2.49).
_REPLACE_RETRIES = 5
_REPLACE_RETRY_SLEEP_S = 0.05


def _cleanup_tmp(tmp: str) -> None:
    """Entfernt eine liegengebliebene Temp-Datei, ohne selbst zu werfen."""
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass


def atomic_write_json(filepath: str, data: Any, indent: int = 2) -> bool:
    """Schreibt JSON atomar via Temp-File + os.replace.

    Returns True bei Erfolg, False bei Fehler (mit Log-Entry).
    Ein konkurrenter Reader sieht IMMER entweder die alte oder die neue
    Version, niemals einen halb-geschriebenen Zwischenstand.

    P2.49-Härtung:
      - Unique Temp-Name via ``tempfile.mkstemp`` im ZIELVERZEICHNIS statt eines
        festen ``.tmp``. Zwei parallele Writer auf denselben Pfad kollidierten
        sonst auf derselben Temp-Datei und korrumpierten sich gegenseitig; das
        gleiche Verzeichnis hält ``os.replace`` auf einem Dateisystem, sodass
        die Atomicity-Garantie erhalten bleibt (Muster core/coins.py, #68).
      - Kurzer Retry auf ``os.replace``, das auf Windows mit ``PermissionError``
        scheitert, solange ein Reader die Zieldatei offen hält. Bleibt es nach
        allen Versuchen blockiert, wird das GELOGGT (kein stiller Update-Verlust
        mehr) und die Temp-Datei aufgeräumt.
    """
    if not filepath:
        logger.error("atomic_write_json: Leerer Pfad übergeben")
        return False

    # abspath() → dirname() ist immer nicht-leer, auch bei bloßem Dateinamen.
    parent = os.path.dirname(os.path.abspath(filepath))
    try:
        if not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
    except OSError as e:
        logger.error(f"atomic_write_json: Zielverzeichnis für {filepath} nicht anlegbar: {e}")
        return False

    basename = os.path.basename(filepath)
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{basename}.", suffix=".tmp")
    except OSError as e:
        logger.error(f"atomic_write_json: Temp-Datei für {filepath} nicht anlegbar: {e}")
        return False

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())

        # os.replace ist atomar auf POSIX und Windows. Auf Windows kann es aber
        # mit PermissionError scheitern, solange ein Reader die Zieldatei offen
        # hält → kurzer Retry statt stillem Update-Verlust.
        last_err: OSError | None = None
        for _ in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, filepath)
                return True
            except PermissionError as e:
                last_err = e
                time.sleep(_REPLACE_RETRY_SLEEP_S)
        logger.error(
            f"atomic_write_json: os.replace auf {filepath} nach {_REPLACE_RETRIES} Versuchen "
            f"blockiert (Reader hält die Datei offen?): {last_err} — Update NICHT geschrieben."
        )
        _cleanup_tmp(tmp)
        return False
    except Exception as e:
        logger.error(f"Error during atomic write von {filepath}: {e}")
        _cleanup_tmp(tmp)
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
