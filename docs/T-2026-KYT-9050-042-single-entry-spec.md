# T-2026-KYT-9050-042 — Single-Entry-Posting (Entry-2-Zeile raus, außer ROM)

**Status:** Anforderung 1 von T-042 (Operator-Auftrag Michi, 2026-07-26).
**Typ:** Fleet-weite Änderung der Signal-Emission. Kein Deploy in diesem PR.

## Intent

Die Fleet handelt DCA: Market-Entry auf `entry1`, Nachkauf-Limit auf `entry2`, SL
hinter `entry2`. T-043/T-044/T-045 haben risiko-adjustiert gezeigt, dass genau
dieser Nachkauf schadet — Arm B (nur `entry1`, Original-SL) hebt den Sharpe bei
**15 von 17** Bots mit belastbarer Stichprobe (Median-Drag B−A **+0,073**),
konsistent über S/R-, Pump/Dump-, Momentum- und Pattern-Detektoren, und auf dem
längsten verfügbaren Fenster (MIS1-168H, 2,5 Monate) sogar verstärkt (Sharpe
0,06 → 0,16).

Michis Entscheid: Arm B live schalten — und zwar **ausschließlich über das
Posting**. Die Signal-Berechnung bleibt in allen Bots unverändert; es verschwindet
nur die `🏦 Entry 2`-Zeile aus der geposteten Nachricht, sodass Cornix die volle
Größe auf `entry1` füllt.

**Ausnahme ROM1** (`28_signal_orchestrator.py`): behält sein `entry2`. ROM1 war in
T-045 einer der beiden Bots ohne DCA-Schaden (Drag +0,014) und der DCA gibt ihm
messbaren MaxDD-Schutz (10 → 14,5 % ohne). ROM1 rechnet sein `entry2` ohnehin
selbst (`ROM1_ENTRY2_OFFSET_PCT`) und baut seine eigene Message — es hängt nicht
an den Upstream-Bots.

## Akzeptanzkriterien (binär testbar)

- [x] **AK1** — Keine der Cornix-Emissions-Sites außer ROM1 schreibt noch eine
  `🏦 Entry 2`-Zeile: `core/signal_post.py`, `7_pattern_detector.py`,
  `9_ai_sr_bot.py`, `10_pump_dump_detector.py`, `11_ai_mis_bot.py`,
  `12_ai_ats_bot.py`, `13_ai_rub_bot.py`, `14_ai_atb_bot.py`,
  `15_ai_master_bot.py`, `16_smc_forex_metals_bot.py`, `18_ai_abr1_bot.py`,
  `25_smc_ml_sniper.py`, `handlers/open_handler.py`.
  *Test:* Source-Pins je Datei + Verhaltenstest gegen `post_ai_signal` (gebaute
  Message enthält genau eine Entry-Zeile).
- [x] **AK2** — ROM1 postet weiterhin `Entry 2`.
  *Test:* Source-Pin auf `28_signal_orchestrator.py` + bestehende
  `build_rom1_cornix_message`-Tests bleiben grün.
- [x] **AK3** — Geometrie und DB unverändert: `entry2` wird weiter berechnet, der
  SL bleibt, wo er war (hinter `entry2`), und `ai_signals.entry2` wird weiter
  geschrieben.
  *Test:* Verhaltenstest prüft die `INSERT INTO ai_signals`-Parameter von
  `post_ai_signal` / `post_shadow_ai_signal` auf unverändertes `entry2`.
- [x] **AK4** — Die Nicht-Cornix-Info-/HTML-Messages zeigen kein `Entry 2` mehr
  (`11_ai_mis_bot.py`, `25_smc_ml_sniper.py`).
  *Test:* Source-Pins.
- [x] **AK5** — Das ROM1-Gating verkraftet Messages **ohne** Entry-2-Zeile:
  `parse_cornix_signal` parst sie weiterhin und liefert `entry == entry1`.
  *Test:* neuer Pin in `backtest/test_signal_orchestrator.py`.
- [x] **AK6** — Regel 4 unverletzt: pro Signal weiterhin genau EINE
  Cornix-parsebare Message.
  *Test:* bestehende Doppel-Post-Pins bleiben grün.
- [x] **AK7** — `python -m pytest backtest/test_*.py` und
  `python tools/regression_guard/guard.py verify|smoke` ohne neue Fehler
  gegenüber dem Vorzustand. Gemessen gegen einen Baseline-Lauf auf `origin/main`
  (f59cf8a) im eigenen Worktree: **Baseline 41 failed / 1521 passed**, **Branch
  42 failed / 1528 passed**. Failure-Listen-Diff: **keine** neue Regression, die
  einzige zusätzliche Zeile ist `test_watchdog_hang::test_probe_real_child_finds_an_open_log`
  — ein Flake aus den zwei parallel laufenden Suiten (probet die offenen Dateien
  eines echten Kindprozesses; isoliert 20/20 grün, und der Test liest keine der
  geänderten Dateien). Guard: `smoke` OK (6 Fixtures, Perturbation gefangen),
  `verify` OK (24 Fixtures) — kein Golden-Refresh nötig, die Indikator-Engine ist
  nicht berührt.

## Out of Scope

- **Cornix-Sizing** — teilt Cornix die Margin auf die Entry-Targets auf, fährt die
  Fleet nach dieser Änderung halbe statt voller Größe. Das ist Cornix-Config und
  liegt beim Operator (Michi), nicht im Code.
- **ROM1** — bleibt vollständig unangetastet.
- **`legacy_trainers/zzz.py`** — eingefrorene Provenienz, wird nie ausgeführt.
- **RRR-/`avg_entry`-Anzeige** in `11_ai_mis_bot.py` / `25_smc_ml_sniper.py`: die
  Info-Message rechnet ihr RRR weiter gegen `(entry1+entry2)/2`. Die Zahl ist nach
  dieser Änderung nicht mehr die real gehandelte — Berechnungen bleiben laut
  Auftrag unangetastet, deshalb hier bewusst gelassen und als Follow-up notiert.
- **Trailing-Close-Bot** (Phase C / Anforderung 2 von T-042) — eigener Schritt.
- **Deploy / Fleet-Restart** — Michi-Entscheid, nicht Teil dieses PRs.
