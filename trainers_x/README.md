# trainers_x — maintained (fixed) trainers from `Documents\_X`

**Distinction from `legacy_trainers/`:** `legacy_trainers/` preserves the original trainers
unchanged as provenance proof (Audit Step 3). `trainers_x/` contains the **corrected**
versions — this is the code used for retraining. The original files in
`Documents\_X` were fixed identically (this folder is the versioned copy of that;
the only difference: the DB password here via `os.getenv("DB_PASSWORD")`).

Context: Task T-2026-CU-9050-016 (Batch E), audit points P0.12 / P1.29 / P1.30 / P1.31.

## Files

| File | Fixes |
|---|---|
| `BT2-Datagrepper-for-ML.py` (ABR1-Datagrepper) | **P0.12:** pandas_ta columns via prefix matching instead of exact names (previously 11/18 features constant 0) + hard ValueError on missing column. **P1.31:** workers report `ok/no_data/error`, abort at <80% coin coverage, skips logged. **New:** constancy assertion over the finished dataset; max. 2 workers at BELOW_NORMAL priority (live fleet on the same host). |

The in-repo trainers `qm_ml_trainer.py` / `smc_ml_trainer.py` (repo root) are versioned anyway
and were fixed directly (P1.29/P1.30/P1.31).
