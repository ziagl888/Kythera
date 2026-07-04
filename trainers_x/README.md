# trainers_x — gepflegte (gefixte) Trainer aus `Documents\_X`

**Abgrenzung zu `legacy_trainers/`:** `legacy_trainers/` konserviert die Original-Trainer
unverändert als Provenienz-Beweis (Audit Step 3). `trainers_x/` enthält die **korrigierten**
Versionen — das ist der Code, mit dem neu trainiert wird. Die Original-Dateien in
`Documents\_X` wurden identisch gefixt (dieser Ordner ist die versionierte Kopie davon;
einziger Unterschied: DB-Passwort hier via `os.getenv("DB_PASSWORD")`).

Kontext: Task T-2026-CU-9050-016 (Batch E), Audit-Punkte P0.12 / P1.29 / P1.30 / P1.31.

## Dateien

| Datei | Fixes |
|---|---|
| `BT2-Datagrepper-for-ML.py` (ABR1-Datagrepper) | **P0.12:** pandas_ta-Spalten per Prefix-Matching statt Exakt-Namen (vorher 11/18 Features konstant 0) + hartes ValueError bei fehlender Spalte. **P1.31:** Worker melden `ok/no_data/error`, Abbruch bei <80% Coin-Abdeckung, Skips geloggt. **Neu:** Konstanz-Assertion über den fertigen Datensatz; max. 2 Worker mit BELOW_NORMAL-Priorität (Live-Fleet auf demselben Host). |

Die In-Repo-Trainer `qm_ml_trainer.py` / `smc_ml_trainer.py` (Repo-Root) sind ohnehin
versioniert und wurden direkt gefixt (P1.29/P1.30/P1.31).
