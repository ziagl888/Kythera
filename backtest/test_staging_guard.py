"""Pins core.staging_guard against the 2026-07-14 clobber (T-2026-KYT-9050-006).

Repro of the real defect: `retrain_from_replay` wrote
`staging_models/td_xgboost_model_1h.pkl`, and a later `smc_ml_trainer` run wrote
the SAME path — silently destroying the walk-forward artifact. DB-free.
"""

import os
import sys
import tempfile
import unittest

import joblib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.staging_guard import (  # noqa: E402
    OVERRIDE_ENV,
    assert_no_foreign_overwrite,
    read_artifact_trainer,
)

REPLAY_TRAINER = "tools/retrain_from_replay.py"
LEGACY_TRAINER = "smc_ml_trainer.py"


def _dump(path, trainer=None, meta=True):
    payload = {"model": object(), "features": ["a"], "optimal_threshold": 0.5}
    if meta:
        payload["meta"] = {"trainer": trainer} if trainer else {}
    joblib.dump(payload, path)


class TestStagingGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "td_xgboost_model_1h.pkl")
        self.addCleanup(self.tmp.cleanup)
        os.environ.pop(OVERRIDE_ENV, None)
        self.addCleanup(lambda: os.environ.pop(OVERRIDE_ENV, None))

    def test_blocks_the_2026_07_14_clobber(self):
        """Legacy trainer must not silently overwrite a replay-retrain artifact."""
        _dump(self.path, REPLAY_TRAINER)
        with self.assertRaises(SystemExit) as cm:
            assert_no_foreign_overwrite(self.path, LEGACY_TRAINER)
        msg = str(cm.exception)
        self.assertIn(REPLAY_TRAINER, msg)
        self.assertIn(LEGACY_TRAINER, msg)

    def test_blocks_the_reverse_direction_too(self):
        _dump(self.path, LEGACY_TRAINER)
        with self.assertRaises(SystemExit):
            assert_no_foreign_overwrite(self.path, REPLAY_TRAINER)

    def test_same_trainer_rerun_passes(self):
        _dump(self.path, REPLAY_TRAINER)
        assert_no_foreign_overwrite(self.path, REPLAY_TRAINER)

    def test_missing_file_passes(self):
        assert_no_foreign_overwrite(self.path, REPLAY_TRAINER)

    def test_fail_open_on_unknown_provenance(self):
        """No meta / no trainer stamp must not block a legitimate retrain."""
        _dump(self.path, trainer=None)
        assert_no_foreign_overwrite(self.path, REPLAY_TRAINER)
        _dump(self.path, meta=False)
        assert_no_foreign_overwrite(self.path, REPLAY_TRAINER)

    def test_fail_open_on_unreadable_artifact(self):
        with open(self.path, "wb") as fh:
            fh.write(b"not a pickle")
        self.assertIsNone(read_artifact_trainer(self.path))
        assert_no_foreign_overwrite(self.path, REPLAY_TRAINER)

    def test_override_env_allows_deliberate_replacement(self):
        _dump(self.path, REPLAY_TRAINER)
        os.environ[OVERRIDE_ENV] = "1"
        assert_no_foreign_overwrite(self.path, LEGACY_TRAINER)

    def test_override_env_only_honours_exactly_one(self):
        _dump(self.path, REPLAY_TRAINER)
        os.environ[OVERRIDE_ENV] = "yes"
        with self.assertRaises(SystemExit):
            assert_no_foreign_overwrite(self.path, LEGACY_TRAINER)


if __name__ == "__main__":
    unittest.main()
