# tools/audit — Analyse-Skripte der Audit-Steps 2–8

Read-only-Skripte, mit denen die Befunde in `audit_reports/` erhoben wurden (2026-07-03, Live-VPS).
Alle DB-Skripte lesen die Credentials aus Env-Vars (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, Host/Port
localhost:5432 hardcoded) und öffnen die Connection **readonly**. Interpreter: die Live-venv
(`crypto_trading_bot_v2\.venv`) hat alle Abhängigkeiten (psycopg2, pandas, xgboost, joblib).

| Skript | Step | Zweck / Report |
|---|---|---|
| `ast_diff.py` | Diff | AST-Vergleich Kythera ↔ Live-Verzeichnis (formatierungs-invariant) |
| `ast_diff_commit.py` | Diff | AST-Vergleich Live ↔ beliebiger Kythera-Commit (`python ast_diff_commit.py <sha>`) |
| `step2_analysis.py` | 2 | Kalibrierung, Per-Modell-WR, Vokabular-Check, Regime-Flaps → `STEP2_DB_VERIFICATION.md` |
| `step2_part2.py` | 2 | RSI-Formel-Check, POC-Broadcast, Coverage/Gap-Census, Whale-Files |
| `step4_results.py` | 4 | Per-Bot/Strategie-Ergebnisse (erste Fassung, inkl. Duplikat-Entdeckung) → Report 14 |
| `step4b_results.py` | 4 | Deduplizierte Ergebnisse + Classic-Auswertung (maßgebliche Zahlen) → Report 14 |
| `step5_hypotheses.py` | 5 | Konfluenz-, Regime-, AIM1-Fade-, FIFO-Tail-Hypothesen → Report 15 |
| `step6_orchestrator.py` | 6 | Gate-Raten, Whitelist-Qualität, Regime-Dauern, Auto-Close-Bewertung → Report 16 |
| `step7_monitor_replay.py` | 7 | First-Touch-Replay des Monitor-Scorings gegen 5m-Kerzen → Report 17 |
| `inspect_models.py` | 3 | MIS1-pkl-Introspektion (Features, Klassen, Thresholds) → Report 13 |
| `live_parity.py` | 3 | MIS1 End-to-End-Parity-Test Bot-Feature-Bau ↔ Modelle (liest DB) |
| `tree_splits.py` | 3 | Split-Count-/Schwellen-Analyse der MIS1-Booster (Ticker-Leakage-Beweis) |

Hinweise:
- Die Zahlen in den Reports sind Snapshots vom 2026-07-03; erneutes Ausführen liefert aktuelle Werte.
- `step7_monitor_replay.py` funktioniert nur für Zeiträume, in denen 5m-Kerzen vorliegen (~30 Tage Retention).
- `step4b_results.py` ist die Referenz für Performance-Zahlen (dedupliziert); `step4_results.py` nur historisch.
