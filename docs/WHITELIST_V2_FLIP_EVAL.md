# Whitelist-v2 Flip-Evaluation (T-2026-CU-9050-069)

**Tool:** `tools/whitelist_v2_flip_eval.py` · **Läuft nur auf dem VPS** (braucht Live-DB, strikt read-only) · **Zweck:** Datengrundlage für Michis Flip-Entscheid v1→v2 des Whitelist-Gates (T-2026-CU-9050-048, MODEL_INTENT §23).

## Intent

Seit dem T-068-Deploy (2026-07-11) schreibt `27_bot_regime_analyzer` die Shadow-Spalten `whitelisted_v2`/`reason_v2` (Netto-Expectancy-Untergrenze mit EB-Shrinkage) parallel zum live gelesenen v1-Gate (`wr_bot >= wr_overall`). Dieses Tool beantwortet die vier Fragen aus T-069:

1. **Divergenz-Matrix** — auf welchen Zellen entscheiden v1 und v2 unterschiedlich, in welche Richtung?
2. **Counterfactual-PnL** — was hätten die Divergenz-Fälle im First-Touch-Replay (047-Scorer-Mechanik) gebracht/gekostet?
3. **Volumen-Effekt** — Gate-Rate v2 vs. v1 auf dem echten Signal-Traffic, ROM1-Trades/Tag-Prognose.
4. **Entscheidungsgrundlage** — Zahlen für Flip ja/nein/Parameter-Nachjustierung. Die Empfehlung selbst schreibt die VPS-Session, der Flip ist Michis Entscheid (Stop-B gültig: kein Mehrwert → v1 bleibt).

## Akzeptanzkriterien (binär testbar)

- [ ] **AK1 Divergenz-Matrix:** Jede Zelle des Whitelist-Snapshots wird in genau eine Klasse eingeordnet — `both_open`, `both_block`, `v2_would_block` (v1 open / v2 block), `v2_would_open` (v1 block / v2 open), `v2_missing` (Spalte NULL). Summe der Klassen = Zellenzahl. — Test: `test_divergence_matrix_*`
- [ ] **AK2 Traffic-Klassifikation:** Jedes Gate-Event (forwarded via `wl_reason`, suppressed via `reason`-Suffix nach `bot_not_whitelisted:`) wird deterministisch als flip-affected (zell-entschieden: `wr_above_overall`, `counter_trend_specialist`, `insufficient_data`, `wr_below_overall`, `counter_trend_insufficient`) oder flip-unaffected (`no_whitelist_entry`, `whitelist_stale:*`, `*fallback*`, NULL) eingeordnet — Fallback-Pfade ändern sich durch den Flip nicht. — Test: `test_classify_*`
- [ ] **AK3 v2-Join:** Flip-affected Events werden über `(pretty_name(bot), regime, alt_context, direction)` gegen den Snapshot gejoint; fehlende Zelle (`cell_missing`) und NULL-v2 (`v2_missing`) werden gezählt, nie still verworfen. — Test: `test_classify_missing_*`
- [ ] **AK4 Eine Geometrie-Quelle:** Counterfactual-Scoring läuft ausschließlich über die T-047-Mechanik (`tools.rom1_counterfactual.score_row`/`load_1h` → `compute_rom1_trade_params` + `simulate_exit`), keine nachgebaute Geometrie (X-R1). — Test: Import-Assertion `test_reuses_047_scorer`
- [ ] **AK5 Drift-Metrik:** Für zell-entschiedene Events wird die Übereinstimmung „aufgezeichnete v1-Entscheidung (Event) vs. v1 im heutigen Snapshot" berechnet und berichtet — sie quantifiziert den Fehler der Snapshot-Näherung (siehe Caveats). — Test: `test_drift_*`
- [ ] **AK6 Volumen-Rechnung:** Gate-Raten v1/v2 und Trades/Tag-Prognose sind reine Funktionen der Klassifikations-Zähler. — Test: `test_volume_*`
- [ ] **AK7 Read-only:** `conn.set_session(readonly=True)`; das Tool enthält kein INSERT/UPDATE/DELETE. — Review + Grep
- [ ] **AK8 Artefakte + Sichtbarkeit:** JSONL (alle Events inkl. Skips) + Summary-JSON nach `KYTHERA_REPLAY_DIR`; Konsolen-Report enthält Prereq-Checks (Bot-27-Freshness via `MAX(computed_at)`, v2-Spalten-Coverage) und per-Tag-Event-Zähler (macht die Outage-Lücke vom 2026-07-13 sichtbar). — Test: `test_daily_counts` + Lauf-Beobachtung

## Out of Scope

- Der Flip selbst (Gate-Umschaltung in Bot 28 + Restart) — eigener kleiner VPS-Eingriff nach Michi-Go.
- Parameter-Nachjustierung der V2_*-Konstanten — Ergebnis der Auswertung, nicht dieses Tools.
- Jede DB-Schreiboperation, jede as-of-Rekonstruktion historischer Whitelist-Stände (siehe Caveat 1).

## Why Build (Phase 0b)

`tools/rom1_counterfactual.py` (047) bucketet nach v1-Gate-Pfaden und kennt v2 nicht; die Divergenz-Achse v1×v2 und der Snapshot-Join existieren nirgends. Das Tool baut NUR diese Achse neu und delegiert Geometrie+Replay vollständig an 047/walkforward (Extend, kein Neubau).

## Methodik & Caveats (im Report wiederholt)

1. **Snapshot-Näherung:** Der v2-Verdict pro Event kommt aus dem *heutigen* Whitelist-Snapshot, nicht dem Stand zur Signal-Zeit (Bot 28 loggt v2 nicht pro Signal; `bot_regime_whitelist` ist UPSERT-only ohne Historie). Bei ≤7 Tagen Abstand und 30d-Statistikfenstern driftet das langsam; die **AK5-Drift-Metrik misst die Näherung** an v1 (dort sind beide Stände bekannt). Hohe v1-Drift (>15%) ⇒ Snapshot-Zahlen nur als Tendenz lesen, Auswertung ggf. um as-of-Rekonstruktion erweitern.
2. **Regime + Alt-Context der Suppressed-Seite** kommen aus dem kombinierten `regime_at_signal`-String (`"REGIME/ALT"`, geschrieben aus `regime_current` — also exakt der debounced Stand, den der Gate beim Entscheid gelesen hat; kein P2.22-Skew). Nur Legacy-Rows ohne `/` fallen auf den `regime_history`-Lookup zur Signal-Zeit zurück (RAW, P2.22-Skew dort dokumentiert). Die Forwarded-Seite hat `alt_context_at_open` nativ.
3. **Counterfactual statt realisiertem PnL auf BEIDEN Seiten** (auch für tatsächlich geforwardete Trades): gleiche Messlatte, keine Monitor-Label-Abhängigkeit (Report-17-Vorbehalt: Monitor-Scoring nur 63,4% replay-konform).
4. **Kurzes Fenster:** Signale der letzten Tage erreichen den Horizont noch nicht — `open_at_horizon`-Trades zählen mark-to-market in die PnL-Summe, nicht in die WR (047-Semantik). Default-Horizont hier 72h (nicht 168h), passend zum kurzen Shadow-Fenster.
5. **Outage 2026-07-13** (~14h Ingestion tot): per-Tag-Zähler zeigen die Lücke; Bot-27-Freshness-Check zeigt, ob der Analyzer durchlief. Bei dünnem Fenster: Auswertung verschieben statt überinterpretieren.

## Ausführung (VPS-Session, ~17./18.07.)

```
# Schnell (nur Matrix + Volumen, kein Replay):
python tools/whitelist_v2_flip_eval.py --skip-replay

# Voll (mit Counterfactual-Replay der Gate-Events seit Deploy):
python tools/whitelist_v2_flip_eval.py --since 2026-07-11T00:00:00 --horizon-hours 72
```

Output: `KYTHERA_REPLAY_DIR/whitelist_v2_flip_eval_<since-datum>_<horizont>h.jsonl` + `..._summary.json` (parametrisierte Namen — Vergleichsläufe überschreiben einander nicht) + Konsolen-Report. Interpretation: `v2_would_block` mit positiver Counterfactual-Summe = v2 würde Geld wegnehmen; `v2_would_open` mit positiver Summe = v2 würde Geld freischalten; Portfolio-Vergleich v1-Auswahl vs. v2-Auswahl auf identischem Traffic steht am Reportende.
