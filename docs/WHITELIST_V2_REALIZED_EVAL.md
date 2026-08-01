# Whitelist-v2 Flip — realisierte Auswertung (T-2026-KYT-9050-007)

**Tool:** `tools/whitelist_v2_realized_eval.py` · **Läuft nur auf dem VPS** (braucht Live-DB, strikt read-only) · **Zweck:** die Zahlen, an denen Michis Flip-Entscheid v1→v2 des Whitelist-Gates hängt — gemessen an **realisierten** Trades, nicht an einem Replay der Regel.

Das Verdikt selbst steht in `docs/T-2026-KYT-9050-007-whitelist-v2-flip-decision.md`. Der Flip ist und bleibt Michis Entscheidung (OPUS-HANDOFF §6, Gate-Flip).

## Abgrenzung zu `tools/whitelist_v2_flip_eval.py` (T-2026-CU-9050-069)

Das 069-Tool beantwortet dieselbe Flip-Frage über einen **Counterfactual-Replay** der ROM1-Geometrie (T-047-Mechanik: `compute_rom1_trade_params` + `simulate_exit` auf 1h-Kerzen). Das ist die richtige Messlatte, wenn kein echtes Outcome existiert.

Dieses Tool tauscht **ausschließlich die Scoring-Schicht** aus: die Gate-Semantik (welche Pfade der Flip überhaupt berührt), die Divergenz-Klassen und der Snapshot-Join werden aus `whitelist_v2_flip_eval` **importiert**, nicht nachgebaut. Es gibt genau eine Wahrheit darüber, was der Flip ändert. Neu ist nur: statt einer Simulation wird der **tatsächlich geschlossene, vom Monitor gescorte Trade** gelesen.

## Zwei realisierte Messlatten — bewusst getrennt gehalten

| Leg | Quelle | Existiert für | Geometrie |
|---|---|---|---|
| **Trigger-Leg** | `closed_ai_signals` (model = Tag) bzw. `closed_trades_master` (strategy = Tag) | **beide** Gate-Seiten | die des Quell-Bots |
| **ROM1-Leg** | `closed_ai_signals` (model = `ROM1`) | **nur** die geforwardete Seite | ROM1s eigene (P1.10) |

Das Trigger-Leg ist die einzige **symmetrische** Messung: ein vom Gate geblocktes Signal wurde trotzdem in den eigenen Channel des Bots gepostet und von den Monitoren gescort — es hat also ein echtes Outcome, auch ohne ROM1-Trade.

Das ROM1-Leg ist das **echte Geld**, aber es existiert strukturell nur auf der forwarded-Seite. `v2_would_open` — die Signale, die v2 zusätzlich durchlassen würde — hat **prinzipiell kein ROM1-Leg** und kann keins bekommen. Das ist keine Datenlücke, die man schließen könnte; das ist die Grenze der Frage.

> **Falle (gemessen, nicht übernommen):** `closed_trades_master.strategy` ist die kanonische Realized-Quelle für die **klassischen Detektoren** (`Fast In And Out`, `Volume Indicator`, `Support Resistance`, `5 Percent`, `Main Channel`) — dort und nur dort. **ROM1 steht nicht in `closed_trades_master`** (0 Zeilen, gemessen 2026-08-01); ROM1 und alle AI-Bots leben in `closed_ai_signals`. Das Tool liest deshalb beide Tabellen und dedupliziert `closed_ai_signals` über den Report-14-Survivor-Key gegen die 357k-Duplikat-Falle.

## Akzeptanzkriterien (binär testbar)

- [x] **AK1 Klassifikation geerbt:** die Flip-Klassen (`both_open`, `both_block`, `v2_would_block`, `v2_would_open`, `v2_missing`, `cell_missing`, `unaffected`) kommen per Import aus `tools/whitelist_v2_flip_eval.py`; dieses Tool definiert keine eigene Gate-Semantik. — Test: Import-Assertion + `test_flip_delta_*`
- [x] **AK2 Zeit-Domäne gemessen:** pro Tag wird die naive Zeitspalte unter BEIDEN Lesarten (UTC / `LEGACY_WRITER_TZ`) gegen die Gate-Events gematcht; die Lesart mit mehr Treffern gewinnt, beide Trefferzahlen stehen im Report. — Test: `test_twin_index_detects_legacy_domain`, `test_twin_index_detects_utc_domain`, `test_pick_domain_*`
- [x] **AK3 1:1-Zuordnung:** ein geschlossener Trade wird höchstens EINEM Gate-Event zugeordnet (greedy, kleinstes |Δt| zuerst); Kollisionen werden gezählt und berichtet, nie doppelt verbucht. — Test: `test_claim_nearest_never_reuses_a_trade`
- [x] **AK4 Eine Realized-Definition:** PnL/WR/Outcome kommen aus `core.realized_pnl` + `tools/fleet_realized_audit` (T-115/T-032-Definition: target-gestaffelter unlevered Move, WR = TP1-Touch, LEGACY/zensiert ausgeschlossen). Keine eigene Mathematik. — Test: `test_realized_from_ai_*`, `test_realized_from_classic_*`
- [x] **AK5 Nichts wird still verworfen:** Events ohne Twin (`no_twin`), ohne ROM1-Leg (`no_rom1_leg`, `not_forwarded`) und ohne Zelle (`cell_missing`) sind gezählte Klassen; `n_with_leg`, `censored_n` und `lev_n` stehen in jeder Zeile. — Test: `test_summarize_legs_counts_missing_legs_separately`, `test_attach_trigger_legs_marks_unmatched`
- [x] **AK6 Asymmetrie explizit:** die suppressed-Seite bekommt nie ein ROM1-Leg, und der Report sagt warum. — Test: `test_suppressed_side_never_gets_a_rom1_leg`
- [x] **AK7 Read-only:** `conn.set_session(readonly=True)`, kein INSERT/UPDATE/DELETE im Tool. — Review + Grep
- [x] **AK8 Aufschlüsselung nach Bot × Richtung** für beide divergenten Klassen, sortiert nach |Σ Move%|. — Test: `test_by_bot_direction_splits_and_sorts`
- [x] **AK9 Sauber vs. drift-kontaminiert:** jede divergente Klasse wird in `v1_agree` (heutige v1-Zelle passt zur aufgezeichneten Entscheidung — sauberer v1-vs-v2-Vergleich) und `v1_drifted` (Zelle hat sich seither bewegt — der „Unterschied" vergleicht zwei v1-Stände) gesplittet und getrennt ausgewiesen. — Test: `test_agreement_split_separates_drifted_events`
- [x] **AK10 Divergenz nach v1-Pfad:** der divergente Traffic wird nach dem aufgezeichneten v1-Pfad aufgeschlüsselt (`insufficient_data` = Default-Open-Krücke vs. `wr_above_overall` = Entscheidung auf Merit). — Test: `test_path_breakdown_splits_crutch_from_merit`

## Out of Scope

- Der Flip selbst (`SELECT whitelisted` → `whitelisted_v2` in `28_signal_orchestrator.get_whitelist_decision`) und der Orchestrator-Restart. Gate-Flip = Michi (harte Regel, OPUS-HANDOFF §6).
- Nachjustierung der `V2_*`-Konstanten in `27_bot_regime_analyzer.py`.
- Jede as-of-Rekonstruktion historischer Whitelist-Stände (siehe Caveat 1) und jede DB-Schreiboperation.

## Methodik & Caveats (der Report wiederholt sie)

1. **Snapshot-Näherung — und ihre gemessene Güte.** `bot_regime_whitelist` ist UPSERT-only ohne Historie, `bot_regime_performance` ebenfalls, und Bot 28 loggt pro Signal nur den v1-Pfad (`wl_reason` / `reason`), nie den v2-Verdikt. Der v2-Verdikt pro Event stammt deshalb aus dem **heutigen** Snapshot. Der T-031-Befund „die historische Whitelist ist nicht rekonstruierbar" gilt unverändert — das Tool umgeht ihn nicht, es **quantifiziert** ihn: die v1-Drift (aufgezeichneter Gate-Pfad vs. heutige v1-Zelle) misst den Fehler auf der einzigen Achse, auf der beide Stände bekannt sind. Die Drift wächst mit der Fensterlänge; **Fenster deshalb immer mit Drift lesen**, nicht ohne.
2. **Drift kontaminiert die Klasse, nicht nur die Genauigkeit (AK9).** Eine Flip-Klasse vergleicht die *aufgezeichnete* v1-Entscheidung mit der *heutigen* v2-Zelle. Passt die heutige v1-Zelle nicht mehr zur aufgezeichneten Entscheidung, vergleicht die Klasse zwei verschiedene Zellstände — sie misst dann Zell-Bewegung, nicht v1-gegen-v2. Gemessen im Mai/Juni-Fenster: **jedes einzelne** `v2_would_open`-Event war drift-kontaminiert. Deshalb ist `v1_agree` die belastbare Teilmenge; `v1_drifted` steht daneben, nicht drin.
3. **v2 ist auf dem Trigger-Leg IN-SAMPLE gefittet.** `27_bot_regime_analyzer` baut `bot_regime_performance` aus den geschlossenen Trigger-Trades der letzten `REFERENCE_WINDOW_DAYS = 30` Tage, und `_v2_whitelist_decision` entscheidet eine Zelle allein aus deren `avg_pnl_pct`/`pnl_stddev`. Ein Lauf innerhalb dieses Fensters misst v2 also gegen die Daten, auf die v2 angepasst wurde — dass v2 dort Zellen mit negativ realisierten Trigger-Trades blockt, ist weitgehend eine Umformulierung des Anpassungskriteriums, **kein unabhängiger Beleg**. Unabhängig sind (a) das ROM1-Leg und (b) ein Lauf mit `--until` vor dem Fensterbeginn. Der Report setzt das Caveat automatisch, wenn sich die Fenster überlappen (`in_sample_overlap`).
4. **Zwei Zeit-Domänen in derselben Spalte.** Gemessen am 2026-08-01: `orchestrator_open_trades.opened_at`, `orchestrator_suppressed_signals.ts` und die **ROM1**-Zeilen in `closed_ai_signals` tragen UTC; die Zeilen der Bots in `closed_ai_signals`/`closed_trades_master` tragen `Europe/Bucharest`-Wanduhr (+3h im Sommer). `KYTHERA_R3_CUTOVER_UTC` ist auf dem VPS **nicht gesetzt** (uniform-utc-Modus) — die Domäne hängt also am Writer, nicht an einem Datum. Ein Join, der das ignoriert, matcht 0,0 % der Events (gemessen). Das Tool entscheidet die Lesart pro Tag aus den Daten und weist beide Trefferzahlen aus.
5. **Zensur durch den Orchestrator selbst.** ROM1-Trades, die per `AUTO_CLOSE_ON_REGIME_CHANGE` geschlossen wurden, tragen `CLOSED_REGIME_CHANGE` und fallen unter die `_CENSOR_FRAGMENTS`-Regel von T-032 (weder Win noch Loss). Auf der ROM1-Seite ist damit ein großer Teil der Legs **zensiert** — die Spalte `zensiert` steht in jeder Tabelle, und die ROM1-Zahlen dürfen nicht als Vollerhebung gelesen werden.
6. **Trigger-Leg ≠ ROM1-Leg (P1.10).** Der Gate entscheidet auf der Statistik des Trigger-Bots, gehandelt wird ROM1-Geometrie. Die beiden Messlatten können sich im **Vorzeichen** widersprechen; sie tun es in der Auswertung tatsächlich. Wer nur eine liest, liest die falsche Frage.
7. **Fallback-Traffic ist flip-neutral.** `no_whitelist_entry`, `whitelist_stale:*`, `regime_is_transition:*`, `regime_unstable:*` und NULL-`wl_reason` laufen unter v2 identisch (der Flip tauscht nur den 4D-Zellen-Lookup). Sie werden gezählt (`unaffected`), aber nicht in den Raten-Vergleich gerechnet — nur in die Trades/Tag-Prognose, als konstanter Sockel.
8. **`lev`-PnL ist exact-only.** Fehlende/unparsbare Hebel führen zum Ausschluss der Zeile, nicht zu einem Default (`core.realized_pnl.parse_leverage`). Die Coverage steht als `(n)` hinter jeder Σ-lev-Zahl; der unlevered Move ist die coverage-robuste Metrik.

## Ausführung (VPS-Session)

```
python tools/whitelist_v2_realized_eval.py --since 2026-07-11T00:00:00                          # volles Shadow-Fenster
python tools/whitelist_v2_realized_eval.py --since 2026-07-25T00:00:00                          # kurz, weniger Drift
python tools/whitelist_v2_realized_eval.py --since 2026-05-15T00:00:00 --until 2026-07-02T00:00:00   # out-of-sample
```

Der CPU-Guard aus `walkforward_sim` bricht bei >90 % Systemlast ab. Der VPS steht dauerhaft bei 100 % (gemessen); für diesen read-only-Lauf gibt es deshalb `--cpu-wait-min N` (warten) und `--force-on-busy` (trotzdem laufen, BELOW_NORMAL-Priorität). Die gemessene Last beim Start steht als `cpu_at_start_pct` im Summary — ein Lauf behauptet nie Headroom, den er nicht hatte.

Output nach `KYTHERA_REPLAY_DIR`: `whitelist_v2_realized_eval_<since-datum>.jsonl` (alle Events inkl. Skip-Gründen), `..._summary.json` (alle Aggregate) und `....md` (Report mit der Bot × Richtung-Aufschlüsselung).
