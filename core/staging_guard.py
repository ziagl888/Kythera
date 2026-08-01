"""Provenance guard for artifacts in ``staging_models`` (T-2026-KYT-9050-006).

Two trainer generations write the SAME file name into the SAME staging dir:

    tools/retrain_from_replay.py:423   STAGING_DIR/{strategy}_xgboost_model_{tf}.pkl
    smc_ml_trainer.py:376              STAGING_DIR/{prefix}_xgboost_model_{tf}.pkl

On 2026-07-14 that collision silently destroyed a completed retrain: the four
walk-forward TD/BB artifacts written between 02:47 and 05:21 were overwritten at
05:21-05:23 by a legacy ``smc_ml_trainer`` run. Only the ``retrain_*_stats.json``
files survived, so the retrain could be *measured* afterwards but never promoted.

The guard makes that overwrite loud instead of silent. It is deliberately
fail-open: an unreadable or provenance-less artifact must not block a legitimate
retrain — the goal is to catch the cross-generation collision, not to police the
directory.

Override for a deliberate replacement:  KYTHERA_ALLOW_TRAINER_OVERWRITE=1
"""

from __future__ import annotations

import os

OVERRIDE_ENV = "KYTHERA_ALLOW_TRAINER_OVERWRITE"


def read_artifact_trainer(path: str) -> str | None:
    """``meta.trainer`` of an existing artifact, or ``None`` if not determinable.

    Fail-open by design: a missing file, an unpickleable artifact (foreign
    library versions) or a dict without ``meta.trainer`` all yield ``None``.
    """
    if not os.path.exists(path):
        return None
    try:
        import joblib

        data = joblib.load(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return None
    trainer = meta.get("trainer")
    return str(trainer) if trainer else None


def assert_no_foreign_overwrite(path: str, trainer: str) -> None:
    """Refuse to overwrite an artifact that a DIFFERENT trainer produced.

    ``trainer`` is the writer's own ``meta.trainer`` stamp. Same trainer (a
    re-run) passes; unknown provenance passes; a foreign stamp raises
    ``SystemExit`` unless ``KYTHERA_ALLOW_TRAINER_OVERWRITE=1``.
    """
    existing = read_artifact_trainer(path)
    if existing is None or existing == trainer:
        return
    if os.getenv(OVERRIDE_ENV) == "1":
        print(f"  ⚠️  overwriting {existing}-artifact with {trainer} ({OVERRIDE_ENV}=1): {path}")
        return
    raise SystemExit(
        f"Refuse: {path} was written by '{existing}', this run is '{trainer}'.\n"
        f"Two trainer generations share this file name — overwriting silently "
        f"destroyed a completed retrain on 2026-07-14 (T-2026-KYT-9050-006).\n"
        f"Archive or rename the existing artifact, or set {OVERRIDE_ENV}=1 to "
        f"replace it deliberately."
    )
