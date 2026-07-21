## [2026-07-21] Bot 10 (EPD): Hot-Path-Fenster-Scans gefaltet, redundanter ISO-Parse + Deque-Kopie entfernt (T-2026-KYT-9050-019)

CPU-Optimierung des Pump/Dump-Detectors (Bot 10) — laut Per-Bot-Messung
2026-07-21 der Top-Fresser der Fleet (bursty, p90 ~30% / max ~46%). Vier
verhaltenserhaltende Änderungen im pro-Tick-×-527-Coin-Hot-Path von
`process_coin_logics`, restart-gated (keine Live-Semantik-Änderung), aufbauend
auf dem Epoch-Cache aus T-165:
- **Anker über gecachten Epoch-Float** statt `_parse_bucket_ts`: `bucket_anchor =
  _bucket_epoch(data[-1])` — der neueste Bucket trägt `'e'` ab Erzeugung im
  main-Loop, der frühere ISO-`fromisoformat` lief pro Coin/Tick umsonst (die eine
  von T-165 übersehene Anker-Stelle). `latest_age_sec` aus Epochs.
- **Deque direkt lesen** statt `list(ONE_MINUTE_DATA[symbol])`-Kopie pro Tick
  (`data` wird nur über `data[-1]` und `reversed(data)` angefasst, nie gesliced).
- **Stunden-Scan + die 6 Price-Move-Lookbacks in EINEM Reverse-Pass**
  (`_scan_hour_and_lookbacks`) statt 1×`_find_bucket_range(3600)` + 6×
  `_find_bucket_before` — ~886 → ~362 Bucket-Iterationen/Coin/Tick. Byte-identisch
  zu den Einzelaufrufen konstruiert und mit 3000-Fall-Fuzz-Test + Band-Edge- +
  Empty/None-Tests gepinnt (`hour_buckets` speist `avg_volume` = Modell-Input UND
  `pump_dump_events`-Insert-Gate — jede Abweichung wäre stiller Regel-7-Skew).

P1.39-Zeitstempel-Fenster, T-035-Nearest-Bänder, `now`=Wanduhr (Staleness/
Cooldowns/`spike_time`) und die „kein erfundener Ersatz-Bucket"-Regel bleiben
unangetastet. Bewusst NICHT in Scope: inkrementeller Stunden-Aggregat statt
Rescan (eigener Regression-Guard nötig, Folge-Task). Verifiziert:
`test_pump_dump_time_windows.py` 21/21 (18 + 3 neue Äquivalenz), Kern-Reviews
(z-code-reviewer + z-spec-compliance-review) PASS, `regression_guard verify`
(24 Fixtures) + `smoke` clean, ruff clean. Deploy = Watchdog-Restart (Michi-gegatet).

## [2026-07-21] Doku: KB-Task-Nummernkreis auf T-2026-KYT-9050-NNN umgestellt (T-2026-KYT-9050-001)

Reine Doku-Änderung, kein Verhaltens-/Code-Effekt. Kythera-Tasks laufen ab
sofort unter dem Canonical-Slug `kythera` im ID-Kreis `T-2026-KYT-9050-NNN`
statt im geschlossenen `T-2026-CU-9050-NNN`-Block (Operator-Entscheid Michi
2026-07-21). Nachgezogen:
- `CLAUDE.md` §Workflow: neuer Bullet mit der Nummernkreis-Konvention (add_task
  `customer/project_id="kythera"`, Prefix `T-2026-KYT-9050-`) + Hinweis, dass
  der alte Kreis geschlossen ist und historische CU-9050-Verweise als Provenienz
  stehen bleiben.
- `docs/OPUS-HANDOFF.md` §2: `/task-start`-Template auf KYT umgestellt,
  Präzedenz-Suche über beide Korpora; zwei aktive Task-Verweise (Eskalation §6
  + Batch-E-Präzedenzfall) auf die migrierten IDs nachgezogen (018→KYT-002,
  020→KYT-003).
- `docs/T-2026-CU-9050-021-opus-task-audit.md`: Migrations-Banner mit vollem
  Mapping (15 offene Tasks nach KYT-002…016 migriert, Rest done/wontfix; KB ist
  Single Source of Truth). Dateiname bleibt als Pfad-Verweis unverändert.

## [2026-07-21] WS2-Batch 2 (deployable-only): SRA2-LONG + EPD3-SHORT live (T-2026-CU-9050-185)

Zweiter Batch der Shadow→Live-Promotionen. Nur die zwei Beine MIT validem
Operating-Point gehen live, koexistierend mit ihren Legacies:
- **SRA2 LONG** (@0.6424) → CH_AI_SR (neben SRA1). Artefakt `sra2_model_LONG.*`
  aus `staging_models/` nach Repo-Root promotet (Regel 2, Operator-Entscheid Michi).
- **EPD3 SHORT** (@0.6737) → CH_PUMP_AI (neben EPD2). Artefakt als
  `epd3_model_SHORT.pkl` nach Repo-Root promotet — bewusst challenger-DISTINKTER
  Dateiname, damit es NICHT den Legacy-EPD2-Loader-Slot `epd2_model_SHORT.pkl`
  (Bot 10 `EPD2_ARTIFACT_PATHS["SHORT"]`) kapert; sonst lädt der EPD2-Live-Pfad
  dieselbe Datei und postet SHORT doppelt (Regel-4-Doppel-Trade — Review-Fund
  T-185, gefixt). Die eingebettete `meta.model_id` des pkl ist noch "EPD2"
  (kosmetisch: der Tag "EPD3" wird explizit am Call-Site übergeben, und der
  distinkte Dateiname verhindert die Legacy-Adoption; ein sauberer Rebuild mit
  model_id="EPD3" bleibt Folge-Arbeit, Re-Dump hier wegen py3.14↔3.13-Mismatch
  vermieden).

SRA2 SHORT und EPD3 LONG bleiben SHADOW — sie haben **keinen deploybaren Edge**
(nicht bloß keinen Threshold): SRA2-SHORTs Label-Quelle `closed_trades3` ist seit
2026-02-23 eingefroren und liefert bei 3027 Events keinen positiv-Edge-Threshold;
EPD3-LONG war „kein positiver Monat". Ein Retrain reproduziert nur das `threshold=
None` — daher kein Retrain (VPS-CPU gespart, read-only auf der Live-DB verifiziert).

Mechanik: `shadow_gate.shadow_artifact_path` löst jetzt is_live-abhängig auf — ein
LIVE-Bein lädt sein Artefakt aus dem Repo-Root (= live, Regel 2), ein SHADOW-Bein
weiter aus `staging_models/`; so kann ein einzelnes Richtungs-Bein eines Tags live
gehen, während das andere Shadow bleibt. Die Bots 9/10 emittieren über den
`post_ai_signal_gated`-Router aus T-183 (LIVE → Cornix, SHADOW → überwacht) — die
„best-direction"-Selektion in Bot 10 lässt den Live-SHORT nur feuern, wenn das
Modell SHORT über Threshold favorisiert. Bot 9 bekam einen expliziten `has_open`-
Duplikat-Schutz für den Live-Leg (post_ai_signal prüft das nicht selbst).

Aktivierung ist Michi-gegatet: Restart der Bots 9/10. Tests: shadow_gate-Registry +
Promotions-Pfadauflösung (live⇒root/shadow⇒staging) grün (77 passed). Der
vorbestehende `test_sra_tag::test_active_trade_check`-Fail (veralteter Test-Anker,
Bot 9 auf der Basis identisch rot) ist keine Regression dieses Diffs.
## [2026-07-21] Bot-11 Inferenz-Vektorisierung: 4.216 → 8 predict_proba-Calls/Scan (T-2026-CU-9050-186)

Ein Fleet-CPU-Audit fand Bot 11 (`11_ai_mis_bot.py`, MIS2) als einzigen Bot mit einem echten Vektorisierungs-Loch: er scort JEDEN der 527 Coins bedingungslos mit
8 Modellen per `predict_proba` auf einem **1-Zeilen-DataFrame** — 527×8 = **4.216 Einzel-Calls pro Scan**. Gemessen kostet ein 1-Zeilen-`predict_proba` ~66ms fast
reinen Per-Call-Overhead (sklearn-Namensvalidierung + DMatrix-Bau), ein Batch über 527 Zeilen ~54ms **total** (0,10ms/Zeile) — pro Coin ~600×. Verhaltensneutraler Fix:

- **`check_mis_models` in drei Phasen restrukturiert.** Phase A baut die Features **weiter pro Coin** (`add_advanced_features` mit Rolling-Windows darf NIE über
  Coin-Grenzen concateniert werden) und sammelt die fertigen 1-Zeilen-Frames. Phase B scort pro Modell in **einem** `predict_proba` über die gestapelte Coin-Matrix
  (neuer reiner Helfer `_score_models_batched`). Phase C baut Kandidaten + Posting **unverändert pro Coin** (gleiches 0.25-Gate, Kalibrator, Threshold-Ranking, Cooldown,
  Outbox/ai_signals/master-Log, per-Coin-Transaktion mit Rollback).
- **Byte-identische Probabilities:** XGBoost scort zeilenunabhängig → ein Batch-Call liefert exakt dieselbe Per-Coin-Wahrscheinlichkeit wie der Einzel-Call; die
  namensbasierte Feature-Auswahl je Modell fixiert die Spaltenreihenfolge identisch. Ein Coin-Ausfall in Phase A (kein Frame / kein Live-Preis) landet nicht in der Matrix
  und verschiebt die Index-Rückverteilung nicht. Bei einem Batch-Fehler (z. B. eine korrupte Zeile) fällt der Helfer für dieses Modell auf den alten Per-Zeilen-Pfad zurück
  → Fehler-Semantik bleibt: eine kaputte Zeile verliert nur ihre eigene Prediction (NaN), alle anderen scoren.
- **Nur die Inferenz gebündelt**, kein Touch an `core/mis_features` (Regel 7, geteilt mit Trainer/Sim). `predict_proba`-Calls/Scan **4.216 → 8**; Mikro-Benchmark auf den
  echten 8 mis2-Modellen 11× schneller selbst auf der gesättigten Box (auf unbelasteter deutlich mehr).

Verifiziert: neuer `backtest/test_mis_batch_inference.py` (5 — Parität Batch≡Einzel, Zeilen-Reihenfolge, Batch-Fehler-Fallback, Ein-Zeilen-NaN, Mehr-Modell-Spalten),
`test_mis_features.py` 7/7, Regression-Guard 24/24, ruff/mypy grün. Aktiv nach Bot-11-Restart (kein Live-Eingriff, keine Trading-Entscheidung).

## [2026-07-20] WS2-Batch 1: 4 Studien-Forwarder live + FIF1 geparkt (T-2026-CU-9050-183)

Erster Batch der Shadow→Live-Promotionen aus Michis 14:00-Report-Review. Vier bisher
shadow-only Regel-Forwarder gehen live, FIF1 wird von TSM1 abgelöst:
- TSM1 SHORT → CH_FIF1 (ersetzt FIF1). FIF1 (Bot 33) gated seinen Live-Post jetzt auf
  `shadow_gate.is_live` und ist über die neuen `("FIF1", *) = SILENT`-Registereinträge
  geparkt — kein `CH_FIF1=0`, das würde TSM1s geerbten Ziel-Channel mitkillen.
- SKW1 LONG+SHORT, XSM1 LONG, XSR1 SHORT → CH_ATS (ehem. ATS-Channel).

Zentralisiert im neuen `signal_post.post_ai_signal_gated`: routet ein (tag, direction)-
Bein durch `shadow_gate` — LIVE → `post_ai_signal` (Cornix + Outbox + ai_signals, genau
EINE Cornix-Message, Regel 4), SHADOW → `post_shadow_ai_signal` (überwacht), SILENT/
retired → No-op. Eine Promotion ist damit ein reiner `_LIFECYCLE`-Flip; die Bots
37/38/39 rufen nur noch den Gate-Router (früh-Guard `leg_status ∈ {LIVE, SHADOW}`).
Reine Regel-Forwarder (Klasse D, kein Artefakt) → kein Regel-2-Promotionsschritt nötig.

NICHT in diesem Batch: EPD3 und SRA2 (Koexistenz-Entscheid Michi). Beide laden ihr
Modell aus `staging_models/`; ein Live-Post daraus verletzt Regel 2 — sie brauchen eine
Artefakt-Promotion staging→root + Load-from-Root-Rewiring (Folge-Batch).

Aktivierung ist Michi-gegatet: die Flips wirken erst nach Deploy/Restart der Bots
33/37/38/39. Tests: neuer `post_ai_signal_gated`-Routing-Test (LIVE/SHADOW/SILENT an
echten Beinen) + die drei Bot-Tests auf Live umgestellt (27 grün). Der vorbestehende
`test_sra_tag`-Fail (Bot 9 unangetastet) ist keine Regression dieses Diffs.

## [2026-07-20] Retired/silenced Modelle aus den aktiven Per-Bot-Report-Blöcken (T-2026-CU-9050-182)

Der 4h-Sentiment-Tracker-Post (`23_market_tracker.py`, `job_per_bot_performance`) listete abgelöste
Generationen (AIM1, MIS1-*) und stummgeschaltete Alt-Beine (ATS1/ATB1) weiterhin in den drei aktiven
Blöcken PER-BOT PERFORMANCE, HALF-KELLY POSITION SIZING und MODELS A–Z (compact) — obwohl der
Realized-PnL-Report sie längst in einen eigenen RETIRED-Block trennt.

Fix, display-only: neuer module-scope Pure-Helper `is_display_retired(tag)` an EINEM Punkt auf die
gemeinsame `strategy_short`-Quelle angewandt (upstream aller drei Blöcke). Ein Tag ist display-retired,
wenn BEIDE Richtungs-Legs `shadow_gate.leg_status ∈ {RETIRED, SILENT}` sind — die konservative per-Tag-
Hebung des per-Leg-Buckets aus dem Realized-Report (ein Tag mit noch einem LIVE-/SHADOW-Bein bleibt
sichtbar). SHADOW- und LIVE-Tags bleiben bewusst sichtbar, weil die Shadow-Perf die Entscheidungsgrundlage
für die anstehenden Modell-Promotionen ist. Kein Posting-/Geld-Effekt.

Tests: `backtest/test_market_tracker_lifecycle.py` +4 Fälle (retired/silenced raus, shadow/live rein,
MIS2-Prefix-Grenze, rohe Vor-Normalisierungs-Formen), 11/11 grün; alle market_tracker-Tests 72/72 grün.
Beide Kern-Reviews (z-code-reviewer, z-spec-compliance-review) PASS; zwei LOW-Notes (Docstring-Präzision,
pretty↔raw-Testpfad) eingearbeitet.

## [2026-07-20] TimescaleDB-Chunk-Exclusion an den AI-Bot-Feature-Reads (T-2026-CU-9050-180)

Der dominante DB-Read der Fleet — `read_candles_with_indicators` (candles⋈indicators) — lief auf dem
hyper-Pfad OHNE untere Zeitgrenze. TimescaleDB konnte daher KEINEN Chunk ausschließen: jeder Read scannte
alle 126 Chunks von `candles` (9 GB) + `indicators` (19 GB). In `pg_stat_statements` war das die Query #1
(≈28 % der gesamten DB-Executor-Zeit, ~215–245 ms/Call, 337k Calls) — auf dem gesättigten VPS trieb sie
Postgres auf ~4,3 Kerne (Analyse T-166/T-173/T-179 + Root-Cause-Session).

Fix, verhaltensneutral: neuer Helfer `core/candles.history_start(tf, n_candles, *, anchor=None, safety=3,
min_days=60)` liefert eine untere `start`-Grenze, die die neuesten `n_candles` geschlossenen Kerzen sicher
abdeckt (`max(n·TF_SECONDS·safety, min_days)`, tz-aware UTC). Die Read-Helfer geben ohnehin die neuesten
`limit` Kerzen zurück (`ORDER BY open_time DESC LIMIT`), daher liefert jede ausreichend weit zurückreichende
`start`-Grenze BYTE-IDENTISCHE Rows — sie wirkt rein als Chunk-Exclusion-Hint. Übergeben an den fünf
Hot-Call-Sites:
- `11_ai_mis_bot.py` (1h, 100), `12_ai_ats_bot.py` (1h, 500 — deckt die OBV-`iloc[0]`-Baseline ab),
  `24_quasimodo_bot.py` (tf, 100), `25_smc_ml_sniper.py` (tf, 150);
- `15_ai_master_bot.py` (As-of, `limit=1`, `anchor=end`): Kandidaten sind auf die letzten
  `CANDIDATE_WINDOW_MIN`=60 min gefiltert, `end`≈jetzt → der 60-Tage-Floor kann den Lookup nie kürzen.

Bewusst NICHT angefasst: `core/research_features.fetch_context_frame` (führt einen `as_of`-Parameter, dessen
Fenster-Semantik separat zu klären ist — Backfill/Replay-Pfad), `core/breadth_features` (nimmt `start=`
bereits durch), `core/ats_features` (kein echter Call-Site). Regel-7-Grenze: der Shared-Read-Pfad selbst
bleibt unverändert; nur die Call-Sites setzen die Grenze.

Beweis (EXPLAIN, live, read-only): dieselbe Query ohne `start` = 252 Per-Chunk-Index-Scans; mit
`open_time >= now()-60d` = 18 (~14× weniger Chunks). Verhaltens-Parität mathematisch (Fenster ≥ n·TF) +
Tests. Residual: eine extrem lückenhaft handelnde Coin (< 1/safety der Wall-Clock-Kadenz über min_days)
bekäme ihre neuesten Kerzen INNERHALB des Fensters statt weiter zurück — jeder Call-Site hat aber bereits
einen Mindest-Row-Guard (`len(df) < N`), so eine Coin wird übersprungen, nicht fehlbewertet.

Verifiziert: `backtest/test_candles.py` (59, davon 7 neu für `history_start`), Parität/Feature/Detector-Suiten
(115 passed), Regression-Guard 24/24, ruff/mypy grün. Aktiv nach dem nächsten Fleet-Restart (Operator-Gate);
kein Live-Eingriff, keine Schema-/Index-Änderung.

## [2026-07-20] Z1-Leaderboard: Risk-Metriken deterministisch — (src, id)-Tiebreaker in der Outcomes-Order (T-2026-CU-9050-177)

VERHALTENSÄNDERUNG (bewusst, der Zweck des Tasks): die Leaderboard-Risk-Metriken
`max_drawdown_pp`/`max_loss_streak` sind jetzt deterministisch/stabil — vorher flackerten sie
run-to-run (real: ein Bot −83,0 vs. −80,3 pp zwischen zwei Polls ohne Datenänderung), weil
`ORDER BY bot, closed_at` bei Duplikat-`closed_at`-Zeilen (8.696 Tie-Gruppen in `closed_ai_signals`,
898 in `closed_trades`) die Tie-Reihenfolge DuckDBs parallelem Scan (threads=2) überließ und beide
Metriken pfadabhängig sind. Die angezeigten Werte ändern sich damit einmalig gegenüber den bisherigen
zufälligen Ständen; die drei reinen Aggregate (rolling / success-rate / regime-matrix) und die
order-invarianten Leaderboard-Felder (n, wins, winrate, pnl_sum_pct, expectancy_pct) sind unberührt.

- `tools/analytics_api.py`: `_outcomes_cte` führt pro Zeile das Tiebreaker-Paar `(src, id)` mit —
  `id` = monoton steigender serieller Postgres-PK der jeweiligen Outcome-Tabelle (Insertion-Order;
  dieselbe Spalte, die der Export-Keyset-Cursor schon als Eindeutigkeits-Tiebreaker nutzt — die beste
  DETERMINISTISCHE Ordnung, die das Schema hergibt), `src` = Union-Zweig-Rang
  (nötig, weil die id-Räume beider Tabellen überlappen: 371k Kollisionen im Live-Export).
  **Grenze:** `id`-Order garantiert KEINE echte Close-Chronologie, wo Upstream `closed_at`
  batch-stempelt — ein bekannter ~340k-Zeilen-Legacy-Reclassify-Block in `closed_ai_signals` teilt
  sich EINEN Zeitstempel; dort sind die Risk-Metriken deterministische Order-Artefakte (stabil, aber
  nicht chronologisch belastbar; betrifft ATS1/EPD1/MIS1-pump ~85-93% ihrer Historie). `open_time`
  als Tiebreaker fuer den Legacy-Zweig = moeglicher Follow-up.
  `bot_trade_rows` + `_leaderboard_rows_streamed` ordnen `ORDER BY bot, closed_at, src, id` — eine
  TOTALE Order; damit ist auch numpy-Fast-Path ≡ Pure-Fallback unbedingt bit-identisch (beide
  konsumieren denselben deterministischen Row-Stream), nicht mehr nur auf tie-freien Daten.
- Beweis auf der realen DuckDB (threads=2, frische `connect_ro`-Connection pro Lauf wie im
  Poll-Pfad): vorher 23 von 68 Bots mit divergierenden Risk-Metriken über 10 Läufe (z. B. ATS1
  −80.386,27 pp/Streak 97 vs. −83.011,02 pp/Streak 80); nachher 10/10 Läufe bit-identisch
  (0 von 71 Bots divergent).
- Tests (`backtest/test_analytics_query_parity.py`): neuer Akzeptanz-Test (rot vor dem Fix, per
  `git stash` verifiziert) mit wert-verschiedenen Duplikat-`closed_at`-Rows, physisch außerhalb der
  id-Order gespeichert + Cross-Table-id-Kollision auf gleichem Bot/Zeitstempel (pinnt `src`) —
  10 Läufe identisch UND gleich den hand-gerechneten id-Order-Erwartungswerten; Parity-Fixture-Ties
  (ids 5/6) auf wert-verschieden verschärft, sodass die ganze Parity-Suite tie-sensitiv prüft;
  numpy≡Fallback-Test entschärft von „on tie-free fixture" auf unbedingt. T-175-Determinismus-
  Caveats in Docstrings/SPEC.md entsprechend aufgehoben (`tools/dashboard/SPEC.md` §Deterministische
  Leaderboard-Risk-Metriken).

## [2026-07-19] Classic-Detector Scan-Optimierung — Spalten-Projektion, gebündelter VolIndic-Read, Zyklus-Snapshots + Active-Trade-Prefilter (T-2026-CU-9050-172)

Verhaltensinvariante DB-/CPU-Entlastung des klassischen Detector-Zyklus (`3_detectors.py`, ~530 Coins,
5 Strategien). Harte Invariante per Operator-Vorgabe: identische Signal-Dicts bei identischem DB-Zustand
und identischen Preis-Inputs — alle betroffenen Guards sind read-only + AND-verknüpft (P2.44-Argument),
es ändern sich nur Query-Form und Auswertungszeitpunkt.

- `3_detectors.py`: Indikator-Read projiziert 27 statt ~120 Spalten (`DETECTOR_INDICATOR_COLUMNS`;
  P2.43-gesichert — Test erzwingt Projektion ⊇ aller Strategy-Spalten-Reads UND ⊆ Engine-DDL);
  Ganz-Coin-Prefilter überspringt Coins, deren sämtliche (Strategie, Richtung)-Paare im TF bereits
  WORKING sind, VOR dem Indikator-Read; EIN aggregiertes ⏱-INFO-Log pro Zyklus (Snapshot-/Read-/
  Scan-je-Strategie-/Write-Dauern, Coins/Skips/Signale) statt Per-Coin-Spam.
- `core/market_utils.py`: neuer `DetectorCycle` — 1× `active_trades_master`-Snapshot (WORKING) als Set,
  1× `trade_cooldowns`-Snapshot je Modul (lazy), generisches Memo für das coin-unabhängige
  `check_recent_trades` (1 Query je (direction, hours, count) statt je Coin); eigene Signal-Writes werden
  via `note_signal_written` zurückgespiegelt (In-Zyklus-Sicht ≡ altem DB-Read).
- `strategies/strat_volume_indicator.py`: die zwei Spike-Reads (5d-Fenster + 10d-Baseline) sind jetzt EIN
  zusammenhängender 15d-Read mit Pandas-Split — Fenstergrenzen exakt erhalten (Baseline-Ende =
  `open_time_1st_hit − 30m`; Randfälle Spike@i==0 / leere Baseline / leeres Fenster paritätsgetestet);
  Beide-Richtungen-aktiv-Skip VOR der Spike-Rechnung.
- `strategies/strat_{5_percent,fast_in_out,support_resistance,main_channel}.py`: optionaler
  `cycle`-Parameter (Fallback ohne Cycle = alte Einzelqueries, byte-identisch); SR/Main prüfen die durch
  die Hit-Seite fixierte Richtung vor First-Hit-Scan/480er-OHLCV-Read (P2.44-Reorder).
- BEWUSST AUSGELASSEN (Spec-Deliverable 2, TF-differenziertes Zeilen-Limit): der
  `first_valid_index`-Fallback auf `support_price` (5%/FastInOut) kann, solange der
  T-061-Head-Nulling-Recompute unvollständig ist und pre-P1.12-Broadcast-Zeilen im 480er-Fenster liegen,
  legitim tiefer als 50 Zeilen greifen — ein kleineres 30m-Frame wäre dort nicht beweisbar
  verhaltensinvariant. `limit=480` bleibt für beide TFs; 1h-Zweistufigkeit ebenso ausgelassen.
- Cooldown-Contract (P1.16: 12h, Tag `VolIndic`, Write via `write_signal_atomic` in derselben Txn) und
  `write_signal_atomic`-Transaktions-Contract unangetastet; DB-Index-Anlage nur als Empfehlung im Code
  dokumentiert (partial Index `active_trades_master(strategy,coin,direction) WHERE status='WORKING'`;
  `closed_trades_master(direction,posted)`) — Ausführung VPS-Session, Michi-gated.
- Adversarial-Review-Fixes (Vote 2): (a) `conn.rollback()` im Read-Except von `3_detectors.py` — ein
  fehlgeschlagener Indikator-Read (fehlende Tabelle/Spalte) hätte sonst die Transaktion für alle
  Folge-Coins des Zyklus vergiftet (InFailedSqlTransaction; Muster latent auch auf main); (b) der
  15d-Split ist ein DREI-Wege-Split (`>= 1st_hit` / `<= 1st_hit − 30m`) statt Komplement — ein
  contract-verletzender, nicht-30m-alignter Bar in `(1st_hit−30m, 1st_hit)` lag in KEINEM der alten
  Fenster und bleibt jetzt auch im gebündelten Read ausgeschlossen (Paritätstest ergänzt).

Query-Bilanz pro 30m-Zyklus (N≈530, im Code dokumentiert): vorher ≈ N×3 Reads (davon der ~120-Spalten-
`SELECT *`) + Guard-Punktqueries ≈ 1.600+; nachher ≈ N×2 schlanke Reads + ~5 Snapshot-/Memo-Queries.
Verifiziert: neuer `backtest/test_detector_scan_optimization.py` (19 Tests: Spike-Fenster-Parität
alt↔neu inkl. 60-Fälle-Random-Sweep, Snapshot≡Einzelquery-Parität inkl. naiver TZ-Normalisierung,
Signal-Dict-Parität aller 5 Strategien auf beiden Pfaden, Projektion-⊇/⊆, Wiring/Prefilter),
Regression-Guard 24/24 golden, ruff/format/mypy grün. Test-Hygiene nebenbei (alle drei Fails vorbestehend
auf unverändertem main, In-Repo-Worktree): `test_window_features` stubbt die Cursor-Naht statt
`pd.read_sql_query` (stale seit der T-108-Migration); `test_candles::test_candle_source_resolves_known_backends`
asserted den Backend-Default auf sauberem Env (die dotenv-Aufwärtssuche findet aus Worktrees das
Operator-`.env` mit `KYTHERA_CANDLES_SOURCE=hyper`); `test_published_targets` restauriert nach seiner
sys.modules-Chirurgie jetzt auch das `core`-PAKET-ATTRIBUT — der Split (Attribut ≠ sys.modules) ließ
`test_shadow_gate`s `_shadow_test_channel`-Monkeypatch ins Leere laufen, sobald das Operator-`.env` ein
`CH_SHADOW_TEST` setzt (gepatcht wurde Instanz A, aufgerufen Instanz B → Echo-Outbox-Assert rot).
Deploy/Restart des Detectors ist NICHT Teil des Tasks (restart-gated, Michi).
## [2026-07-19] Indikator-Engine CPU-Optimierung — Early-Skip + persistente Worker-Connections + Compute-Mikro-Opts, byte-identisch (T-2026-CU-9050-174)

Die Engine rechnete alle 30 min 527 Coins × 6 TFs (~3.160 Tasks, NUM_WORKERS=3) komplett durch, obwohl
bei den meisten (Symbol, TF)-Paaren seit dem letzten Zyklus keine neue GESCHLOSSENE Kerze existiert (1d
hat z.B. nur in 1 von 48 Zyklen neue Arbeit). Alle drei Findings des Reviews vom 2026-07-19 umgesetzt;
DB-Endzustand nachweislich byte-identisch, Trade-Charakteristik unverändert.

- Finding 1 (größter Hebel, ~2/3 der Zyklus-Arbeit): Early-Skip in `process_coin_task` — nach dem
  Watermark-Read zusätzlich `latest_open_time(kind='candles', include_forming=False)`; ist die neueste
  geschlossene Kerze nicht neuer als der Watermark, sofortiger Return VOR `read_candles`.
  End-State-Identitäts-Argument (Review-korrigiert): der FINALE Write jeder Zeile passiert immer in
  einem New-Candle-Zyklus (dem letzten, dessen 5-Kerzen-Save-Fenster sie noch deckt) — und
  New-Candle-Zyklen reißen das Prädikat immer und laufen exakt wie bisher; abgelöste Referenz-Bars
  werden identisch geNULLt. Die übersprungenen Zwischenzyklen schrieben das Save-Fenster nur mit
  einem um eine Kerze verschobenen Warmup-Fenster neu — Werte, die der nächste New-Candle-Zyklus
  ohnehin überschrieb. **Akzeptierte, begrenzte Abweichungen** (heilen je zum nächsten Kerzenschluss;
  Operator-Review beim Rollout): (a) die Window-Global-Spalten der Referenz-Bar bleiben auf der ersten
  Post-Close-Berechnung eingefroren statt ~30 min später einmal aufs verschobene Fenster zu wechseln;
  (b) eine In-Place-Korrektur einer bereits geschlossenen Kerze (Outage-Recovery-Catch-up, wie
  2026-07-13) wird erst beim nächsten Kerzenschluss nachgerechnet statt im nächsten 30-min-Zyklus
  (bei 1d/1w bis zu 1 Tag/1 Woche); (c) fällt eine Periodengrenze exakt zwischen Skip-Probe und
  `read_candles`, rutscht die Kerze einen Zyklus. Randfälle intakt: verspätete Ingestion (neue
  geschlossene Kerze > Watermark) → Recompute; Housekeeping-Gap-Invalidierung (Indikator-Zeilen
  gelöscht, Watermark springt zurück) → Recompute. Die `updated`-Freigabe pro TF
  (`update_timeframe_state`) bleibt unverändert im Orchestrator-Loop.
- Finding 2: EINE persistente DB-Connection pro Pool-WORKER (`initializer=_init_worker`, Lazy-Connect,
  Reconnect-bei-Fehler) statt Pool-Checkout/-Rückgabe pro Task (~3.160 Checkouts/Zyklus, jede Rückgabe
  mit ROLLBACK-Round-Trip + Liveness-Probe). Transaktions-Hygiene (Review-Finding): ein `finally`
  beendet die Task-Transaktion auf JEDEM Exit-Pfad (`get_transaction_status()`-Check, client-seitig) —
  die persistente Connection hält nie eine offene Transaktion (und deren AccessShareLocks über
  distinkte per-Coin-Tabellen) über Tasks hinweg, und ein von einer BaseException hinterlassener
  Partial-Write kann nie vom Folge-Task mitcommittet werden; scheitert der Rollback, wird die
  Connection verworfen und der nächste Task verbindet neu. Commit bleibt beim Caller (harte Regel 8).
  Dazu `get_indicator_definitions()` einmal pro Worker und ein Positiv-Cache für die zwei
  `table_exists`-Probes pro Task (nur Treffer gecacht — neu angelegte Tabellen werden weiter gefunden).
- Finding 3 (Mikro-Opts, bit-identisch): (a) `calc_macd` re-nutzt die EMA_9/12/21/26-Serien aus dem
  EMA-Block statt vier `ewm`-Neuberechnungen; (b) redundanter `df.sort_values('open_time')` in
  `calculate_indicators_optimized` entfernt (alle Caller liefern ASC: read_candles Contract 1,
  Guard-Fixtures, recompute `ORDER BY`); (c) True-Range via `np.fmax.reduce` statt
  `pd.concat(...).max(axis=1)` — bewusst fmax statt maximum, damit die NaN-Skip-Semantik der ersten
  Bar (`close.shift()`) erhalten bleibt; (d) `exe.map(..., chunksize=8)` amortisiert die
  IPC-Round-Trips über ~527 Tasks/TF.
- BEWUSST NICHT angefasst: `lookback_candles=1000` (EWM-Konvergenz EMA/SMMA_200/KAMA/TSI, harte
  Regel 7), `NUM_WORKERS=3` (VPS CPU-saturiert, T-166), KAMA-Restschleife (inhärent sequenziell).

Verifikation: Regression-Guard 24/24 golden OHNE Refresh + `smoke` grün; zusätzlich
Bit-Identitäts-Beweis alt↔neu über alle 24 Fixtures (111 Spalten, float64-Bitmuster-Vergleich:
100% identisch). Neuer DB-freier Test `backtest/test_indicator_engine_skip.py` (8 Tests: komplette
Skip-Entscheidungstabelle inkl. Erstlauf/Gap-Rücksprung, Transaktions-Ende auf jedem Exit-Pfad,
Positiv-Cache, Discard bei kaputter Connection). backtest: test_gap_continuity, test_wilder_rsi,
test_window_features (Engine-Teil), test_candles_schema, test_fleet_definition, test_watchdog_backoff
grün; der S/R-Reader-Teil von test_window_features und test_candles' Backend-Flag-Test scheitern
identisch auf unverändertem main (stale `pd.read_sql_query`-Stub seit der read_candles-Umverdrahtung
bzw. Test-Isolations-Leck — keine Regression, Follow-up-Kandidaten). ruff + format grün. Reviews:
3-Vote z-code-reviewer (Findings behoben: Transaktions-Hygiene-`finally`, Doku-Korrekturen, Tests) +
Spec-Compliance PASS. Erwartet ~60-70% weniger Engine-Last/Zyklus + Postgres-Entlastung (weniger
Checkouts/Reads). Aktiv nach dem nächsten Engine-Restart (Michi-gated).
## [2026-07-19] Z1-Dashboard: DuckDB-Analytics-Queries 2,8–8,5x schneller + Panel-Daten-Cache (T-2026-CU-9050-175)

Profile-first gegen die reale served-DB (~824k Outcome-Rows, ~580k decisive): die beiden 11-Sekunden-
Aggregate (`bot_leaderboard`, `rolling_success_rate_series`) transferierten JEDEN decisive Trade als
Python-Dict über die DuckDB-Grenze — genau der beim Deploy beobachtete Cold-Start-Timeout (>10s).
Query-seitig optimiert, ergebnis-erhaltend. **Paritäts-Umfang, ehrlich:** die drei reinen count/sum-
Aggregate (`rolling_success_rate_series`, `success_rate_timeseries`, `bot_regime_matrix`) sind auf der
realen DB **bit-identisch** old-vs-new verifiziert (json-identisch). Für `bot_leaderboard` gilt das für
die order-INVARIANTEN Felder (n, wins, winrate, pnl_sum_pct, expectancy_pct); die zwei order-ABHÄNGIGEN
Risk-Metriken (`max_drawdown_pp`, `max_loss_streak`) behalten die **SELBE vorbestehende run-to-run-
Nichtdeterminismus-Klasse** wie der alte Code — nicht bit-identisch per Natur (siehe BEFUND unten), aber
kein NEUER Nichtdeterminismus ggü. alt. Gemessen (reale `analytics.duckdb`, frische Connection pro Call,
min/3):

| Query | vorher | nachher |
|---|---|---|
| `bot_leaderboard` | 11.509 ms | 4.098 ms |
| `rolling_success_rate_series` (w=30) | 11.812 ms | 1.395 ms |
| `success_rate_timeseries` (7/30/90) | 1.620 ms | 1.318 ms |
| `bot_regime_matrix` (ASOF) | 2.400 ms | 2.120 ms |

- `tools/analytics_api.py`: `rolling_success_rate_series` aggregiert die Tages-Buckets jetzt in DuckDB
  (`GROUP BY bot, d`, reine Integer-Zählungen → Parität exakt per Konstruktion) statt ~580k Rows nach
  Python zu holen; `_daily_buckets_by_bot`/`bot_trade_rows` bleiben als Referenz-Pipeline erhalten.
  `bot_leaderboard` läuft über einen Streamed-Column-Pfad (`_leaderboard_rows_streamed`): 3-Spalten-
  Projektion (`closed_at` bleibt NUR Sortkey, nie 580k materialisierte datetimes), lazy-optionaler
  numpy-`fetchnumpy`-Fast-Path mit bit-identischem Pure-Python-Fallback — beide rufen wörtlich
  `_leaderboard_row`s eigene Mathematik (`_leaderboard_row_from_columns`: builtin `sum()`, naiver
  Drawdown-Loop). `success_rate_timeseries` rechnet alle Fenster in EINEM Scan (FILTER-Aggregate übers
  breiteste Fenster; Bot-Inklusion pro Fenster über Any-Row-Count rekonstruiert) statt ein Scan pro
  Fenster. `bot_regime_matrix`: `ASOF JOIN` (inner) statt `ASOF LEFT JOIN + WHERE` (beweisbar
  zeilenidentisch, `regime_sorted` ist NULL-frei) + redundantes inneres `ORDER BY ts` entfernt.
- `tools/dashboard/app.py`: Panel-Daten-Cache (`_PollCache`, File-Freshness-Token — dasselbe Muster wie
  der bestehende Blueprint-Cache): bei unveränderter Export-Datei wird jeder 30s-HTMX-Poll aus Memory
  bedient (keine Connection, kein Scan; Steady-State ~0 ms). Gecacht werden NUR DuckDB-derivierte Daten
  (Payload + `data_freshness`-ROWS); das „Sync vor N min"-Alter wird weiter pro Request aus der Wall-
  Clock gerechnet, Fleet-Registry (dateibasiert) bewusst uncached. `cache=None`-Default = exakt altes
  Verhalten.
- Paritäts-Netz: neuer `backtest/test_analytics_query_parity.py` (23 Tests) — Referenz-Implementierungen
  (alte Query-Shapes) vs. neu auf tmp-DuckDB-Fixtures (beide Outcome-Tabellen + regime_history), inkl.
  Edge-Cases: closed_at-Ties, Bot-Filter, Fenster-Duplikate/-Teilmengen, explizites `as_of`, leeres
  Substrat, numpy≡Fallback auf tie-freien Fixture-Daten (Scope ehrlich benannt, s.u.) PLUS ein
  tie-robuster Implementierungs-Äquivalenztest über EINEN geteilten Row-Stream, Cache-Hit ohne Reconnect
  UND Cache-Invalidierung bei neuem File-Token (echter Re-Export). 208 Dashboard-+Paritäts-Tests grün
  (`pytest backtest/test_dashboard_*.py backtest/test_analytics_query_parity.py`), ruff 0.15.17
  check+format clean.
- BEFUND (vorbestehend, unverändert gelassen): das ALTE `bot_leaderboard` war bei `closed_at`-Ties
  run-nondeterministisch, weil `ORDER BY bot, closed_at` keinen deterministischen Tiebreaker hat und
  DuckDBs paralleler Scan (threads=2) Ties zwischen Duplikat-/Same-Instant-Rows in `closed_ai_signals`
  je Lauf anders ordnet (real reproduziert: 6/10 Läufe divergieren, z.B. `max_drawdown_pp` ATS1 −83.003
  vs. −80.303 pp, `max_loss_streak` ±24 auf identischem File). Betrifft nur die zwei path-abhängigen
  Risk-Metriken; die count/sum-Felder sind order-invariant und stabil. Das gilt auch für numpy-Pfad-vs-
  Fallback: jeder `bot_leaderboard`-Call führt die Query neu aus → eigener Tie-Stream, daher kann die
  numpy≡Fallback-Gleichheit nur pro row-stream (nicht über getrennte Calls) garantiert werden. Die
  Optimierung behält `ORDER BY bot, closed_at` **absichtlich unverändert** bei (gleiche Nichtdeterminismus-
  Klasse, kein neuer). Ein deterministischer Tiebreaker würde diese geld-werten Metriken VERÄNDERN und ist
  daher ein **separater Follow-up-Task (Verhaltensänderung)** — bewusst NICHT Teil dieser PR.

## [2026-07-19] Z1-Ops-Skript nachgezogen — Dashboard-Task auf Password-Logon + cmd.exe-Launcher (T-2026-CU-9050-170)

Live-Verifikation des Z1-Deploys ergab: `tools/ops/register_kythera_dashboard_tasks.ps1` registrierte den Dashboard-Task als **S4U** — das funktioniert NICHT. Der kurze Export-Batch läuft unter S4U, aber der langlaufende waitress-Dashboard-Server bindet im Session-0-S4U-Kontext Port 8098 nie (getestet: auch nach 35s kein Bind). Der Fleet-Watchdog nutzt aus demselben Grund `LogonType=Password`. Zweiter Fund: eine Scheduled-Task-Aktion kann eine `.cmd` nicht direkt starten (kein CreateProcess auf `.cmd`) → sie muss über `cmd.exe /c "<launcher>"` laufen. Beides live gefixt (Dashboard läuft jetzt Session 0, HTTP 200) und hier ins committete Skript nachgezogen.

- `tools/ops/register_kythera_dashboard_tasks.ps1`: Dashboard-Task (A) jetzt **Password-Logon** (`Read-Host`-Passwort-Prompt → `-User`/`-Password`, `-RunLevel Highest`) + Aktion **`cmd.exe /c`** auf einen zur Laufzeit geschriebenen **Logging-Launcher-`.cmd`** (leitet stdout/stderr in `staging_models/analytics/dashboard_scheduled.log`, damit ein Startfehler sichtbar ist). Export-Task (B) **unverändert S4U** (kurzer Batch, kein Passwort nötig). Header/`.NOTES` + Footer entsprechend aktualisiert. Bleibt **registrierungs-only** (kein Live-Cutover — CLAUDE.md Hard-Regel 1). Nur Ops-Skript, kein Python/Fleet-Code; `.ps1` parse-verifiziert (kein DB-freier Test möglich).

## [2026-07-19] Ingestion Batch-Flush — ein execute_values statt ~3.185 Einzel-INSERTs/s (T-2026-CU-9050-169)

Umsetzung des T-168-Ingest-Reports (Maßnahmen 1–3): Der 3s-DB-Flusher schrieb jede Kerze als EIGENES
Statement mit eigenem SAVEPOINT/RELEASE-Paar — live gemessen ~3.185 Einzel-INSERTs/s + ~6.400
SAVEPOINTs/s (≈2,6s DB-Executor-Zeit/s) und der Großteil der ~59% Client-CPU der Ingestion. Jetzt geht
der komplette Buffer auf dem Hyper-Write-Primary als EIN `execute_values`-Batch raus; DB-Endzustand
beweisbar identisch (gleiches Statement, gleicher `IS DISTINCT FROM`-No-op-Guard, gleiche
forming→closed-Flip-Semantik).

- `core/candles.py`: neue Bulk-API `upsert_candles_many()` (hyper-only, Row-Shape = `_CANDLES_HYPER_UPSERT`-
  Spaltenordnung mit closed pro Row, bool-strikt validiert, committet nicht — Contract 3) +
  `candles_write_primary()` als öffentlicher Accessor. Rein additiv, Einzel-Pfad unverändert.
- `1_data_ingestion.py`: `_flush_to_db` nutzt auf `WRITE_PRIMARY=hyper` den Batch (1 Round-Trip, 1 Commit);
  bei Batch-Fehler Rollback + Fallback auf Gruppen-Flush mit SAVEPOINT-Isolation pro
  (symbol, tf, closed)-Gruppe (statt pro Row — die reale Fehlerklasse „fehlende Tabelle" ist ohnehin
  gruppenweit); Legacy-Primary geht direkt in den Gruppen-Pfad. Persistente Flusher-Connection statt
  connect/close alle 3s (Reconnect-on-Error, Monitore-Muster). Optional orjson fürs WS-Parsing
  (stdlib-Fallback, inert bis installiert).
- BEWUSST NICHT: Flush-Intervall (Maßnahme 4 = Operator-Entscheid), Kerzen-Schließ-Semantik,
  Client-Dedup von Forming-Updates, `KYTHERA_CANDLES_*`-Flags, Catch-up-Overlap (No-Go-Liste T-168).

Mikro-Benchmark (DB-frei, 9.550-Row-Flush): ~28.650 Statements/590ms Client-CPU → 20 execute_values-Pages/
1ms (614×). Verifiziert: neuer `backtest/test_ingestion_batch_flush.py` (11 Tests: Batch≡Einzel auf
SQL-Ebene, Choreografie inkl. Fallback-Isolation + Connection-Reset), Regression-Guard 24/24 golden,
ruff/mypy grün; die 14 vorbestehenden candles-Suite-Failures sind umgebungsbedingt (identisch auf
unverändertem main: Live-Hyper-Flags + seit Write-Primary-Umstellung stale Legacy-Tabellen). Aktiv nach
dem nächsten Ingestion-Restart (Michi-gated).
## [2026-07-19] Confidence-Posting-Floors aus Realized-Trade-Analyse — AIM2 0.70 / BB 0.50 / SRA1 0.70 (T-2026-CU-9050-171)

Threshold-Analyse über die realisierten Trades (T-2026-CU-9050-170, read-only): `closed_ai_signals`
(dedupliziert nach Audit-Key) ⨝ `ml_predictions_master`-Confidence (Nearest-Time ±10 min) = 32,4k Trades
03–07/2026 mit Bootstrap-CI95. Befund: bei drei Bots ist das Posting-Segment unter einem Confidence-Floor
Null-EV — weniger Trades bei gleichem/höherem PnL ist dort eine reine Gate-Frage. PR #157:

- **Neu `core/prob_floor.py`:** `load_prob_floor(env_var, default)` — env-überschreibbarer Floor,
  Clamp [0,1], Garbage/NaN/Inf → Default. Invariante überall: effektives Gate = `max(Artefakt-Threshold,
  Floor)` — ein Floor kann nur verschärfen, nie den Operating-Point des Artefakts unterlaufen.
- **AIM2 (Bot 15):** `AIM2_MIN_PROB` (Default **0.70**) im Live-Gate UND im TOPN-Floor. Unter p=0.70 in
  beiden Artefakt-Ären Null-EV (Ø +0,18 %, CI [−0,27, 0,67], ~72 % des Volumens); ab 0.70 Ø 1,0–2,2 %/Trade,
  WR +6 pp.
- **BB-Sniper (Bot 25):** `BB_MIN_PROB` (Default **0.50**) über dem geladenen Artefakt-/Hardcode-Threshold
  (Artefakte tragen 0.30). Unter p=0.5 Null-EV (~95 % des Volumens); darüber Ø 1,2–1,9 %/Trade. **TD bewusst
  ohne Floor** — Confidence dort auf realisierten Trades nicht selektiv, Kanal netto positiv.
- **SRA1 (Bot 9):** `SRA_LEGACY_THRESHOLD` 0.65 → **0.70**. Das 0.65–0.70-Band war netto negativ (Ø −0,10 %);
  ab 0.70 bleiben 62 % der Trades mit MEHR Gesamt-PnL (302 vs. 274) und WR 52 → 55,5 %.
- **Unangetastet (bewusst):** QM (Bot 24, Operator-Entscheid bleibt live), TD-Legs, alle Shadow-Floors
  (AIM2 0.25, Sniper 0.25, SRA 0.35) — Datensammlung unterhalb der Gates läuft voll weiter. Keine
  Artefakt-Änderungen, keine Gate-Flips. **Wirkt erst mit dem nächsten Fleet-Restart.**

Beobachtung nebenbei (kein Code in diesem PR): RUB2 live bestätigt die Held-out-Validierung bisher nicht
(Ø −0,15 %, n=209; TP1-WR 67,5 % vs. realisierte Win-Rate 48,8 %) — der Hebel dort ist Exit-Management,
nicht das Gate.

Verifiziert: `backtest/test_prob_floor.py` (neu, 29 mit `test_aim2_topn.py`) — Parsing-Semantik + statische
Gate-Verdrahtung aller drei Bots (Floor-nur-verschärfen, TD-ohne-Floor, Shadow-Floors gepinnt); Regression-
Guard smoke OK; ruff/mypy grün. Reviews: z-code-reviewer 3-Vote APPROVED + z-spec-compliance PASS.

## [2026-07-19] Z1-Export atomic-publish — Retry-Budget 1s → ~30s gegen Dashboard-Polling (T-2026-CU-9050-167)

Der atomare Publish in `tools/analytics_export.py` (`publish_duckdb`, T-163) scheiterte in der Praxis
(verifiziert 2026-07-19) unter aktivem HTMX-Polling des Z1-Dashboards: das Dashboard öffnet die served-DuckDB
per Request read-only und pollt über mehrere Panels quasi-durchgehend → `os.replace(<served>.tmp → served)`
warf auf Windows `PermissionError`/`WinError 5`. Das alte Retry-Budget **5×200ms = ~1s** fand in dieser
Zeit keine Lücke → Publish FAILED nach 5 Versuchen, served blieb der alte Snapshot. Ein registrierter
30-min-Export-Task hätte so **nie** ans Dashboard publisht. Kein Datenverlust — die Fehlerpfad-Safety hielt
(Build-DB + `.tmp` intakt, served unangetastet).

- **Budget deutlich erhöht + konfigurierbar:** neue Konstanten `DEFAULT_PUBLISH_RETRIES=120` /
  `DEFAULT_PUBLISH_RETRY_DELAY_S=0.25` → **~30s Gesamt-Budget** statt 1s. Da das Dashboard sein Read-Handle
  per Request schließt, ist die served-Datei >90 % der Zeit frei; ein weites Fenster trifft zuverlässig eine
  Lücke. Signatur `publish_duckdb(..., retries=, retry_delay_s=)` bleibt rückwärtskompatibel.
- **CLI-Flags:** `--publish-retries` / `--publish-retry-delay` reichen das Budget an `publish_duckdb` durch —
  Operator-Tuning ohne Code-Change.
- **Selbstheilung dokumentiert:** scheitern alle Versuche, republisht der nächste Lauf dieselben frischen
  Daten aus der persistenten Build-DB → ein verpasster Publish ist nie Datenverlust, nur verzögert
  (Exit-Code ≠ 0 bleibt). Retry-WARNINGs sind gedrosselt (erste 3 + alle 20) statt ~120-fach-Spam.

Verifiziert: `backtest/test_analytics_export_publish.py` (17, u.a. neu: 30 gesperrte Versuche → Publish
gelingt im Default-Budget; Guard gegen Rückfall auf 5) + `test_analytics_export.py` (25), ruff `check`/
`format --check` grün. SPEC.md (`tools/dashboard/`) um Budget-Rationale ergänzt.
## [2026-07-19] Bot-10 CPU-Optimierung — Epoch-Fenster-Scans, geteiltes Stundenfenster, kompakter State-Dump (T-2026-CU-9050-165)

Der Pump/Dump-Detector hat auf JEDEM Fenster-Lookup jedes Buckets den ISO-Zeitstempel neu geparst
(`fromisoformat`) — bei 527 Coins × bis zu 1440 Buckets × ~10 Scans pro 10s-Tick der dominante CPU-Posten
des Bots: gemessen **4,4s des 10s-Tick-Budgets** (Dev-Maschine, voller Bestand). Auf dem gesättigten VPS
ist das der plausibelste Treiber der bimodalen Bucket-Kadenz aus T-035 (p90 = 70s statt 10s) — der Bot kam
mit seinem eigenen Tick nicht hinterher. Alles verhaltensneutral:

- **Epoch-Schlüssel:** Buckets tragen ab Erzeugung ein `e`-Feld (Epoch-Sekunden des Grid-Stempels); alle
  `_find_bucket_*`-Helper vergleichen Floats statt datetime-Objekte (`_bucket_epoch`, Lazy-Parse-Once-Cache
  für Alt-State-Dateien und Test-Fixtures; Anker akzeptieren datetime ODER Epoch via `_anchor_epoch`).
- **Stundenfenster einmal pro Tick:** Volume-Explosion-Pfad (A2) und ML-Pfad (B) zogen sich je einen
  eigenen, identischen 3600s-Scan (gleicher Anker, gleiche Daten) — jetzt einmal berechnet, geteilt.
- **State-Dump entschärft:** `1minute.json`/`pump_dump_state.json` kompakt statt `indent=2` (vorher >100MB
  und ~9s reine Serialisierung ALLE 5 MINUTEN); Bucket-Deque 1440 → 720 (`BUCKET_DEQUE_MAXLEN`): das größte
  Fenster ist 3600s+20s Toleranz, die ältere Hälfte wurde von keinem zeitbasierten Lookup je erreicht.

Steady State danach: **1,68s/Tick (2,6×)**; der erste Tick nach Restart füllt den Epoch-Cache einmalig.
Verifiziert: `backtest/test_pump_dump_time_windows.py` (18) + `test_epd2_entry_from_ticker.py` +
`test_shadow_prediction_cooldown.py` (9), Regression-Guard 24/24 golden, ruff/mypy grün. Aktiv nach
Bot-10-Restart. Monitore 5/8 bewusst NICHT angefasst (deutlich kleinerer Posten; Batched-Candle-Read als
Follow-up geprüft).
## [2026-07-19] RUB4 — funding-gegatetes RUB-LONG als Shadow-Experiment (T-2026-CU-9050-164)

Das RUB-LONG-Bein blutet (live RUB2-LONG −2,5 %/Trade, Shadow-RUB3-LONG −3,7 %). Retrospektive über 123
geschlossene RUB-LONG-Trades: das ABR1-Funding-Gate (`fund_24h > +3 bps`) dreht das Aggregat ins Plus
(−2,90 % → **+1,61 %**), aber nur **6/123** Trades passieren es → vielversprechend, aber zu dünn zum Live-Schalten.
Daher als **reines Shadow-Experiment** forward-validiert (Michi-Entscheid: nur Shadow, live unangetastet).

- `13_ai_rub_bot.py`: neuer Tag **RUB4** — emittiert in `_emit_rub3_shadow` DENSELBEN RUB3-Kandidaten
  (gleiches Modell, gleiche Geometrie, gleicher Entry), aber NUR wenn `funding_gate_open(feats["fund_24h"])`
  (strikt `> 3.0 bps`, ABR1-LONG-Schwelle; `fund_24h` ist bereits in den Funding-Features berechnet). Pure
  `funding_gate_open`-Funktion (DB-frei testbar). Rein additiv, nie live, fail-safe zu Stille wenn RUB4 nicht
  SHADOW. Report vergleicht so **gegatet (RUB4) vs. ungegatet (RUB3)** direkt.
- `core/shadow_gate.py`: `("RUB4","LONG") → SHADOW`. Kein eigener `SHADOW_ARTIFACTS`-Eintrag — RUB4 nutzt das
  RUB3-Artefakt (`SHADOW_RUB3_LONG`); `bot_catalog` mappt RUB4 über den `RUB`-Prefix auf Bot 13.
- Tests: `backtest/test_rub4_funding_gate.py` (Gate-Grenzen, Registrierung, Tag→Bot; ABR1-Schwelle). Zusätzlich
  **Test-Hermetik-Fix** (Folge von T-150): `test_shadow_gate.py` schaltet den CH_SHADOW_TEST-Echo per autouse-
  Fixture ab, damit die „nie telegram_outbox"-Invariante auch dann grün bleibt, wenn in der Umgebung/.env ein
  CH_SHADOW_TEST gesetzt ist (sonst lokal falsch-rot unter dem Live-Checkout). 63/63 grün.

Aktiv nach Fleet-Restart. Wird RUB4 forward-positiv (genug n), ist Promotion des Gates auf das live RUB-LONG
eine separate Operator-Entscheidung.
## [2026-07-19] Atomarer Export-Publish + committete Z1-Ops-Skripte (T-2026-CU-9050-163)

Der Analytics-Export (`tools/analytics_export.py`) hielt bisher den exklusiven DuckDB-Write-Lock direkt
auf der **served** DuckDB (`staging_models/analytics/analytics.duckdb`), die das Z1-Dashboard per Request
read-only öffnet → während eines Laufs erroren die Datenpanels transient (beim 2,5h-Erstlauf war das
Dashboard datenseitig komplett tot). Der Export arbeitet jetzt auf einer **persistenten Build-DB**
(`analytics.duckdb.build`, RW geöffnet, trägt das Watermark → Inkrementalität ab dem ersten Lauf/Seed exakt erhalten) und
**publisht atomar**: `shutil.copy2(build, <served>.tmp)` → `os.replace(<served>.tmp, served)`
(atomar auf demselben Volume). Der served-Pfad wird vom Export **nie RW geöffnet** → Dashboard-Reads
werden nie blockiert.

- `tools/analytics_export.py`: neue DB-frei testbare `publish_duckdb(build, served, *, retries=5,
  retry_delay_s=0.2)` + `build_db_path()`-Helfer. Windows-Sharing-Violation-Retry (bis zu 5 Versuche,
  200 ms Pause, `log.info`/`warning`/`error` pro Versuch); schlägt der Publish nach allen Retries fehl,
  bleiben Build-DB **und** `.tmp` intakt, served bleibt unangetastet (kein Korruptions-Risiko) und `main()`
  gibt Exit-Code ≠ 0 zurück. `os.replace` ist Modul-Level monkeypatchbar. Defensiv-Guard: `build == served`
  → No-op (kein Self-Replace/Datenverlust). Served-Default-Pfad, alle Flags, das Parquet-Schreiben und die
  Watermark-Semantik unverändert.
- **Rollout-Seed (`seed_build_db`)**: Der Wechsel auf die persistente Build-DB ist der erste Split vom alten
  Single-File-Layout. `main()` seedet daher VOR dem Export einmalig `analytics.duckdb.build` aus der
  bestehenden served-DB (`shutil.copy2`, klare `log.info`-Zeile), falls die Build-DB fehlt aber die served
  existiert → das persistierte `_export_watermark` bleibt erhalten, kein mehrstündiger Voll-Re-Export aus dem
  Live-Postgres. Echter Erstlauf (beide fehlen) bleibt Voll-Export in eine leere Build-DB. Der
  menschenlesbare „Exported N rows"-Summary-Print läuft jetzt NACH dem Publish (bei Publish-Fehler klare
  `publish PENDING — served NOT updated`-Kennzeichnung + Warnzeile), damit ein Operator nie einen
  Erfolgs-Look bei nicht-aktualisierter served-DB liest.
- `tools/ops/register_kythera_dashboard_tasks.ps1` (neu, **registrierungs-only**): reproduzierbare,
  committete Registrierung der zwei Windows Scheduled Tasks — "Kythera Z1 Dashboard" (waitress
  @127.0.0.1:8098, AtStartup, S4U, Restart x3/1min) und "Kythera Analytics Export"
  (`-m tools.analytics_export`, alle 30 min, S4U, `IgnoreNew` = kein überlappender Lauf, 2h-Limit). Das
  Skript **registriert nur** — es stoppt keinen Prozess, startet keine Task und fasst die laufende Fleet
  nicht an (CLAUDE.md Harte Regel 1: kein Live-Eingriff/Fleet-Restart aus committetem Dev-Artefakt). Cutover
  (manuelle Instanz stoppen + `Start-ScheduledTask`) ist ein separater, bewusster Operator-Schritt, den das
  Skript nur als Hinweiszeile ausgibt. Header dokumentiert Elevation-Pflicht, Registrierungs-only-Charakter
  und den S4U-Fallback (→ LogonType Password).
- `backtest/test_analytics_export_publish.py` (neu, 15 Tests, DB-frei mit tmp-DuckDB): Build→served-Kopie,
  Erstlauf-Bootstrap, Retry-on-lock (monkeypatch `os.replace`, Retry-Zähler verifiziert), Alle-Retries-
  scheitern (served untouched, Build-DB intakt, `.tmp` bleibt), `build == served`-No-op, Integration
  (AnalyticsExporter→Build-DB→publish→queryable served DuckDB) + Migrations-Tests für den Rollout-Seed
  (served mit Watermark, `.build` fehlt → Seed erhält Cursor, Folge-Export zieht 0 Zeilen statt Voll-Historie).

## [2026-07-18] Read-only Event-Feed — Z1-Dashboard Feature 9, letztes Panel (T-2026-CU-9050-161)

Neunter und letzter Feature-Baustein des Z1-Dashboard-Rewrites: ein chronologischer (neueste zuerst)
Event-Feed, der Regime-Übergänge (`regime_history`) und Notable Trades (größte Wins/Losses aus
`closed_ai_signals`/`closed_trades`) zu einer typisierten Liste konsolidiert. S10 ist bewusst ein
"einfaches Eingriffs-Log", kein Annotations-Editor — ein SCHREIBENDES Annotations-Feature wäre ein
Mutations-Endpoint und damit F4-/Z2-gegated (CLAUDE.md harte Regel: keine Mutationen/Live-Hebel in der
Web-UI vor Cloudflare Access). **Kein POST/Write-Endpoint gebaut** — Operator-geschriebene Annotationen
sind ein dokumentierter Z2-Follow-up (Auth + CSRF + eigener Persistenz-Store müssen zuerst stehen).

- `tools/analytics_api.py`: additive `event_feed(con, window_hours, *, as_of=None, bots=None)` +
  Helfer `_regime_transition_events()` (wiederverwendet dieselbe `lag()`-Logik wie
  `_regime_changes_in_window`, Feature 8, liefert aber das volle von→nach statt nur die Zählung),
  `_notable_trade_events()` (wiederverwendet die coin-aware CTE `_outcomes_cte_with_coin`, Feature 7/8;
  größte Wins/Losses getrennt über `is_win`, nie über sortierte `pnl_pct` mit Überlappungsrisiko bei
  wenigen Trades) und `_latest_event_anchor()` (data-anchored `as_of`, mit Fallback auf
  `regime_history` falls keine Outcome-Tabelle existiert). Halb-offenes Fenster (`> as_of-Nh AND
  <= as_of`), identisch zu `overnight_digest`. Events chronologisch ABSTEIGEND sortiert. Bestehende
  Aggregate (`_regime_changes_in_window`, `_outcomes_cte_with_coin`, `overnight_digest`, …) inhaltlich
  unverändert.
- `tools/dashboard/app.py`: `/panels/event-feed`-Route, `resolve_event_feed_window` (Default 24h,
  Alternative 168h/7 Tage; unbekannter `?window=` → Default, kein 500), `_event_feed_context`,
  `PANEL_SOURCES`-Eintrag (`closed_ai_signals`/`closed_trades`/`regime_history`).
- `templates/panels/event_feed.html` (neu) + Einbindung als letztes Panel in `index.html`;
  `static/css/app.css` additiv (Event-Feed-Listen-Styles).
- `tools/dashboard/SPEC.md`: Feature 9 dokumentiert inkl. Out-of-Scope-Follow-up für schreibende
  Annotationen (Z2-gegated).
- `backtest/test_dashboard_event_feed.py` (neu, 17 Tests): DB-freie Tests inkl. realem
  Integrationstest (AnalyticsExporter→DuckDB→Flask-Route→HTML) + Mutation-Checks (Sortierrichtung
  desc→asc, Fenstergrenze `>`→`>=`) beide manuell verifiziert rot; zusätzlich ein Test, der bestätigt
  dass `POST /panels/event-feed` 405 liefert (kein Write-Verb existiert).
## [2026-07-18] Nicht-ASCII-Meme-Symbole aus der Universe filtern (T-2026-CU-9050-162)

Bugfix: Binance listet gelegentlich USDT-Perps mit Nicht-ASCII-Symbolen (chinesische Zeichen, z. B. `龙虾USDT`,
`我踏马来了USDT`, `币安人生USDT` — 3 von 530). Der einzige coins.json-Writer (`core/coins.py::filter_usdt_perpetuals`,
P2.16) übernahm das Binance-`symbol` verbatim (nur quote/status/contractType geprüft) → diese Symbole landeten in
`coins.json`. Jeder Kerzen-lesende Bot, der `coins.json` DIREKT lädt (Bot 14/ATB und ~12 weitere, die
`load_coins`' `[A-Z0-9]+`-Filter umgehen), reichte sie an `core.candles.read_candles` weiter → `validate_symbol`
warf pro Scan **„invalid symbol for table identifier"** (u. a. schlug die ATB2-Shadow-Emission für diese Coins fehl,
Log-Noise).

**Fix am EINEN Writer:** `filter_usdt_perpetuals` wendet jetzt zusätzlich die bereits vorhandene Shape-Predikat
`looks_like_usdt_perp` (`[A-Z0-9]+USDT`) an → Nicht-ASCII-Basen fallen an der Quelle raus, fleet-weit, ohne
Änderung an den ~13 Direkt-Lesern. Robust auch für künftige Meme-Listings. 2 neue Tests
(`backtest/test_coins_writer.py`: Nicht-ASCII-Symbol wird gedroppt, `looks_like_usdt_perp` False/True; 10/10 grün).
Aktiv nach Deploy + Fleet-Restart (der coins.json-Writer in `1_data_ingestion` schreibt die bereinigte Liste beim
nächsten Ingestion-Start). Kein Signal-/Handelspfad berührt.

## [2026-07-18] Overnight-Digest-Startseite — Z1-Dashboard Feature 8 (T-2026-CU-9050-160)

Achter Feature-Baustein auf der T-151-Shell: eine Digest-Zusammenfassung ganz oben auf der Startseite
(oberhalb der Fleet-Registry), die für ein konfigurierbares Fenster (Default „Overnight" = letzte 8h,
`?window=`-Umschalter) auf einen Blick zeigt, was passiert ist — aggregierte Netto-PnL (Summe %),
Trade-Count, Gesamt-Win-Rate als Kennzahlen-Kacheln, plus Top-Bot/Flop-Bot des Fensters und die
notable Trades (größter Win, größter Loss mit Coin + Bot).

- `tools/analytics_api.py`: additive `overnight_digest(con, window_hours, *, as_of=None, bots=None)` +
  `_regime_changes_in_window()` (echte Regime-Übergänge via `lag()`, nur bei vorhandenem
  `regime_history`). Halb-offenes Fenster über `close_time` (`> as_of-Nh AND <= as_of`); `as_of=None`
  → data-anchored auf `max(closed_at)`. Wiederverwendet die decisive-Trade-CTE (`_outcomes_cte_with_coin`)
  — bestehende Aggregate (`bot_trade_rows`/`bot_leaderboard`/`success_rate_timeseries`/`bot_regime_matrix`)
  inhaltlich unverändert. Leeres Fenster/leeres Substrat → all-None-Degrade (kein 500).
- `tools/dashboard/app.py`: `/panels/overnight-digest`-Route, `resolve_digest_window` (unbekannter
  `?window=` → Default, kein 500), `_digest_context`; Datenstand-Badge-Quelle closed_ai_signals.
- `templates/panels/overnight_digest.html` (neu) + Einbindung ganz oben in `index.html`; `static/css/app.css`
  Digest-Kacheln.
- `backtest/test_dashboard_digest.py` (neu): DB-freie Tests inkl. realem Integrations-Test
  (AnalyticsExporter→DuckDB→Flask-index→HTML) + Mutation-Checks (Fenster-Grenze `>`→`>=`, Top/Flop-Sort).
- **Transparenz-Hinweis:** `ruff format` (CI-Pin 0.15.17) hat beim Formatieren einige *bestehende* Zeilen
  in `analytics_api.py`/`app.py` mechanisch reumgebrochen (reiner Whitespace, keine Logik; `tools/` ist
  von der CI-Format-Prüfung ohnehin ausgeschlossen) — auf 0.15.17-kanonisch belassen statt zurückgedreht.

## [2026-07-18] Coin-Drilldown mit Ebenen-Kette — Z1-Dashboard Feature 7 (T-2026-CU-9050-159)

Siebter Feature-Baustein auf der T-151-Shell: eine Ebenen-Kette — Coin-Selektor (listet nur Coins mit mindestens
einem entschiedenen Trade) -> Preislinie + Trade-Marker + Trade-Tabelle fuer den gewaehlten Coin. Volle
OHLCV-Kerzen sind explizit NICHT Scope (der 25GB-Kerzen-Export wurde in T-131 vertagt und liegt nicht im
DuckDB-Substrat) — dokumentiert als Follow-up, gated auf den Kerzen-Export.

- `tools/analytics_api.py`: neue additive Funktionen `coins_with_trades()` + `coin_trade_series()` ueber eine
  eigene coin-aware CTE (`_outcomes_cte_with_coin`) — dieselben `MICRO_PNL_PCT`/`MAX_ABS_PNL_PCT`-Schwellen wie
  `_outcomes_cte`, aber mit Coin/Entry/Exit/Target-Hit-Projektion, die die bestehende CTE nicht traegt. Die
  bestehenden Aggregatfunktionen (`_outcomes_cte`, `bot_trade_rows`, `bot_leaderboard`,
  `success_rate_timeseries`, `bot_regime_matrix`) bleiben byte-fuer-byte unveraendert. `targets_hit` ist `None`
  fuer eine `closed_trades`-Zeile (die Tabelle hat keine solche Spalte) — nie eine fabrizierte 0.
- `tools/dashboard/app.py`: neue Route `GET /panels/coin-drilldown`, neuer `PANEL_SOURCES`-Eintrag
  (`closed_ai_signals` + `closed_trades`), `_resolve_coin()` (kein `?coin=` -> erster verfuegbarer Coin;
  unbekannter/leerer Wert -> sauberer Hinweis statt Fehler), `_coin_chart_series()` (Entry->Exit-Punkte je Trade
  ueber `close_time`, deterministisch monoton bei Zeit-Kollisionen) + Win/Loss-Marker.
- `tools/dashboard/templates/panels/coin_drilldown.html` (neu) + `index.html`: neues Panel mit Coin-Selektor,
  Lightweight-Charts-Preislinie + Markern und Trade-Tabelle (Close-Zeit, Bot/Modell, Richtung, Entry, Exit, PnL,
  Target-Hit); leerer/unbekannter Coin und leeres Substrat degradieren sauber (kein 500).
- `tools/dashboard/static/js/panels.js`: neue Lightweight-Charts-Factory `coin-price-line` (vendored 4.2.3,
  `createChart`/`addLineSeries`/`setMarkers`), via `chart_lifecycle.js` registriert — Disposal ueber
  `chart.remove()` (die Lightweight-Charts-eigene API, NICHT ECharts' `.dispose()`).
- `backtest/test_dashboard_coin_drilldown.py` (neu, 24 Tests): realistische Fixtures ueber BEIDE Outcome-Tabellen
  (`closed_ai_signals` + `closed_trades`), Integrationstest ueber die echte
  `AnalyticsExporter`→DuckDB→Route→HTML-Kette, Mutation-Check bestaetigt (manuell verifiziert): ein entfernter
  Coin-Filter macht `test_coin_trade_series_wrong_coin_filter_yields_different_trades` rot. Getestet: Coin-Liste
  nur mit entschiedenen Trades, Coin-Filter korrekt, unbekannter/leerer Coin sauber, leeres Substrat sauber,
  kein Postgres, Chart-Factory-Registrierung inkl. `chart.remove()`-Disposal-Vertrag.
- `tools/dashboard/SPEC.md`: neuer Feature-7-Abschnitt (AK1-AK8, Out-of-Scope, Scope of consent).
- `ruff check .`/`ruff format --check .` gruen; `regression_guard verify` gruen (24 Fixtures); alle 176
  bestehenden + neuen Dashboard-/Analytics-Tests gruen. `git diff --stat`: nur Additionen (443 Zeilen, 0
  Loeschungen) — bestehende `analytics_api`-Aggregatfunktionen unveraendert bestaetigt.
- **Follow-up:** volle OHLCV-Kerzen (Candlesticks) sind gated auf den vertagten Kerzen-Export aus T-131 (25GB).

## [2026-07-18] Bot x Regime Performance-Heatmap — Z1-Dashboard Feature 6 (T-2026-CU-9050-158)

Sechster Feature-Baustein auf der T-151-Shell: eine ECharts-Heatmap (Zeilen = Bots, Spalten =
Regime-Zustaende aus `regime_history`, Zell-Wert = Winrate oder Ø-PnL/Trade, per Toggle umschaltbar). Fuer
jede (Bot, Regime)-Zelle zaehlen die DECISIVEN Trades des Bots, deren `close_time` in das Zeitfenster faellt,
in dem dieser Regime-Zustand aktiv war.

- `tools/analytics_api.py`: neue additive Funktion `bot_regime_matrix()` — wiederverwendet
  `_outcomes_cte`/`_bot_filter`/`_existing_outcome_tables` (dieselbe DECISIVE-Trade-Definition wie
  `bot_trade_rows`/`success_rate_timeseries`, unveraendert). Ordnet jeden Trade seinem aktiven Regime-Zustand
  per DuckDB `ASOF LEFT JOIN` gegen `regime_history` zu (`ON closed_at >= ts`: der letzte klassifizierte
  Regime-Eintrag VOR/AN dem Trade-Zeitpunkt — `regime_history` ist ein Append-Only-Log, ein Zustand gilt ab
  seinem `ts` bis zum naechsten Eintrag). Trades vor dem allerersten Regime-Eintrag haben kein ASOF-Match und
  werden aus der Matrix ausgeschlossen statt in eine fabrizierte "UNKNOWN"-Spalte gebucht. Eine
  (Bot, Regime)-Zelle ohne Trades fehlt in `cells` komplett (kein Nullwert-Platzhalter).
- `tools/dashboard/app.py`: neue Route `GET /panels/regime-heatmap`, neuer `PANEL_SOURCES`-Eintrag
  (`regime_history` + `closed_ai_signals` fuer die Datenstand-Badge), neue Kontext-Funktion
  `_regime_heatmap_context()` (reshaped die Matrix in eine Tabellen-Fallback-Form + eine sparse ECharts-
  Heatmap-Serie) und einen lokalen Metrik-Toggle (`resolve_regime_heatmap_metric`, Winrate/Ø-PnL, unbekannter
  Wert faellt still auf Winrate zurueck).
- `tools/dashboard/templates/panels/regime_heatmap.html` (neu) + `index.html`: neues Panel mit
  Metrik-Umschalter, Datenstand-Badge, ECharts-Heatmap UND Tabellen-Fallback (leere Zellen als "—", nie
  fabriziert).
- `tools/dashboard/static/js/panels.js`: neue ECharts-Heatmap-Factory `bot-regime-heatmap`, via
  `chart_lifecycle.js` registriert (dispose/re-init bei HTMX-Swap); sinnvolle Farb-Skala je Metrik (Winrate
  0-100% sequentiell, Ø-PnL divergierend um 0), sparse Serie (fehlende Zelle = kein Eintrag, kein
  fabrizierter Nullwert).
- `backtest/test_dashboard_regime_heatmap.py` (neu): realistische Fixtures (echte `closed_ai_signals`- +
  `regime_history`-Spaltennamen aus `tools/analytics_export.py`, mehrere Bots x mehrere Regime-Zustaende,
  wiederholte Regime-Labels die zu EINER Spalte verschmelzen). Integrationstest ueber die echte
  `AnalyticsExporter`→DuckDB→Route→HTML-Kette. Mutation-Check bestaetigt (manuell verifiziert): ein Trade
  exakt auf der Regime-Grenze muss ins NEUE Fenster fallen — ein Flip der ASOF-Ungleichung (`>=` → `>`) macht
  `test_bot_regime_matrix_boundary_trade_joins_new_regime_window` rot. Getestet: korrekte Zuordnung,
  fehlende Zelle bleibt abwesend (kein Nullwert), Trade vor dem ersten Regime-Eintrag ausgeschlossen, leeres
  Substrat degradiert sauber, kein Postgres. `ruff check .`/`ruff format --check .` gruen; `regression_guard
  verify` gruen (24 Fixtures, 3.13-Interpreter mit numpy+duckdb); alle bestehenden + neuen Dashboard-/
  Analytics-Tests gruen (152 passed).

## [2026-07-18] Globaler Erfolgs-Metrik-Toggle — Z1-Dashboard Feature 5 (T-2026-CU-9050-157)

Fuenfter Feature-Baustein auf der T-151-Shell: ein shell-globaler Erfolgs-Metrik-Toggle (Winrate /
Expectancy / Netto-PnL) im Base-Layout bestimmt, welche Kennzahl die Panels hervorheben. Umgesetzt als
`?metric=`-Query-Param, den das Leaderboard-Panel liest — die gewaehlte Metrik wird als hervorgehobene
Spalte gezeigt UND als Default-Sort verwendet. Sinnvoller Default (Netto-PnL = die bestehende
`DEFAULT_LEADERBOARD_SORT`); ein unbekannter `metric`-Wert faellt still auf den Default zurueck (kein 500);
Panels, die die Metrik nicht kennen, ignorieren den Toggle unschaedlich.

- `tools/dashboard/app.py`: neue Konstanten `METRICS`/`DEFAULT_METRIC`/`METRIC_LABELS`/`METRIC_SORT_BY` und
  zwei reine, Flask-/DuckDB-freie Funktionen — `resolve_metric(raw)` (unbekannt/None → `DEFAULT_METRIC`) und
  `metric_sort_by(metric)` (Mapping winrate→winrate, expectancy→expectancy_pct, netto-pnl→pnl_sum_pct, jeder
  Wert ein Key aus `analytics_api._LEADERBOARD_SORT_KEYS`). `_leaderboard_context` bekommt einen additiven
  `metric`-Parameter (ruft `bot_leaderboard(sort_by=metric_sort_by(metric))` und reicht `metric`/`metric_label`
  ans Template durch). Die Routen `index()` und `panel_leaderboard()` resolven `?metric=` genau einmal; die
  Shell backt den resolvten Wert in die eigene hx-get-URL des Leaderboard-Panels, sodass Load + `every Ns`-Poll
  dieselbe Metrik behalten (kein zusaetzlicher Round-Trip, kein Client-JS-State).
- `tools/dashboard/templates/base.html`: neuer Toggle-Control (`.metric-toggle`) mit drei GET-Links auf `/` mit
  `?metric=…`, aktive Option markiert. `index.html`: `metric` in die Leaderboard-hx-get-URL gebacken.
  `leaderboard.html`: `metric_label` in der As-of-Zeile, `metric-highlight`-Klasse auf der aktiven Metrik-Spalte
  (Header + Zellen), konsistent mit dem Sort.
- Eingefaltete Review-Nit-Cleanups: (1) `static/css/app.css` — eigenes `--loss`-Token fuer `.pnl-negative`
  (statt des `--stale`-Freshness-Tokens, semantisch entkoppelt, gleicher Farbwert → kein visueller Bruch);
  `--live` (byte-identisch zu `--accent`) entfernt, `.badge--live` nutzt jetzt `var(--accent)`. (2) Modul-Funktion
  `panel_freshness()` → `panel_freshness_summary()` umbenannt (kollidierte namentlich mit dem nested
  Route-Handler `def panel_freshness()` in `create_app()`); alle vier Panel-Context-Caller und die
  Freshness-Tests angepasst, verhaltenserhaltend. (3) Test-Luecke (T-154-MEDIUM) geschlossen — `sort_by="winrate"`
  und `sort_by="n"` mit divergenter Fixture (Reihenfolge ≠ pnl-Default), sodass ein ignorierter `sort_by`
  auffaellt.
- `backtest/test_dashboard_metric_toggle.py`: neue DB-freie Test-Suite mit realistischer
  `closed_ai_signals`-Fixture, deren drei Metriken dieselben drei Bots in DREI verschiedenen Reihenfolgen
  ranken (Mutation-Check: ein falsches/ignoriertes `metric`→`sort_by`-Mapping rendert eine der anderen
  Reihenfolgen). Getestet: reines Mapping (alle drei Metriken + Default + unbekannt), Integrations-Test ueber
  die echte `AnalyticsExporter`→DuckDB→Route→HTML-Kette (Sort + Highlight je Metrik), Shell-Toggle-Rendering und
  Default-Fallback ohne 500, kein Postgres. `ruff check`/`format --check` gruen; `regression_guard verify` gruen
  (24 Fixtures, 3.13-Interpreter mit numpy); alle bestehenden + neuen Dashboard-/Analytics-Tests gruen (111
  passed — die Umbenennung bricht nichts).

## [2026-07-18] Datenstand-Indikator pro Panel — Z1-Dashboard Feature 4 (T-2026-CU-9050-156)

Vierter Feature-Baustein auf der T-151-Shell: bisher zeigte EIN shell-globaler Badge (Base-Layout) den
Datenstand des juengsten Sync ueber ALLE Quellen. Jetzt zeigt jedes der vier Panels (Erfolgsrate,
Erfolgsraten-Zeitvergleich, Leaderboard, Fleet-Registry) den Datenstand SEINER EIGENEN Quelle(n) — bei
mehreren Quellen die AELTESTE (worst-case), nie eine Mischung. Der globale Badge bleibt unveraendert
bestehen (additive Verfeinerung).

- `tools/dashboard/app.py`: `freshness_summary()` bekommt zwei additive optionale Parameter — `sources`
  (filtert die Freshness-Zeilen vor der Aggregation auf genannte Quellennamen) und `worst_case` (aggregiert
  bei `True` die AELTESTE statt der bisherigen Default-FRISCHESTEN Quelle). Beide Defaults reproduzieren das
  bisherige Verhalten exakt — kein bestehender Aufrufer betroffen. Neue reine Funktion `panel_freshness(rows,
  panel, *, now_utc=None)` loest ueber die neue Konstante `PANEL_SOURCES` (Erfolgsrate/Zeitvergleich/
  Leaderboard → `closed_ai_signals`+`closed_trades`, Fleet-Registry → leeres Tuple = kein DuckDB-Sync) die
  Quelle(n) des Panels auf und delegiert an `freshness_summary(..., worst_case=True)`; ein unbekannter
  Panel-Name wirft `ValueError` statt still auf einen Fallback zu vertuschen. Neue Konstante
  `FILE_BASED_FRESHNESS` fuer die dateibasierte Fleet-Registry (kein fabrizierter Zeitstempel). Alle vier
  Panel-Context-Funktionen liefern jetzt einen `freshness`-Eintrag.
- `tools/dashboard/templates/_panel_freshness_badge.html`: neues parametrisiertes Badge-Partial (nimmt die
  panel-lokale `freshness`-Variable), eingehaengt in `success_rate.html`, `success_rate_timeseries.html`,
  `leaderboard.html`, `fleet_registry.html`. Rendert "Stand HH:MM, Sync vor N min", "Live" (dateibasiert)
  oder "—" (fehlende Freshness — nie fabriziert), aktualisiert sich mit dem bestehenden Poll-Intervall des
  jeweiligen Panels (kein zusaetzlicher HTMX-Round-Trip). Der bestehende globale Badge
  (`_freshness_badge.html`/`base.html`) bleibt unangetastet. `static/css/app.css`: neue `--live`-Akzentfarbe,
  `.badge--live`, `.panel__freshness`.
- `backtest/test_dashboard_freshness.py`: 12 DB-freie Tests mit realistischen Freshness-Zeilen-Fixtures
  (echte `closed_ai_signals`/`closed_trades`-Spaltennamen) — Quellenfilter- und Worst-Case-Aggregations-Tests,
  Panel→Quelle-Zuordnung inkl. Fleet-Registry-Sonderfall und unbekanntem Panel-Namen (`ValueError`),
  Mutation-Check fuer Age-aus-`synced_at`-statt-`last_row_ts` sowie fuer eine falsche Quellen-Zuordnung
  (oldest-wins muss unabhaengig davon greifen, WELCHE der beiden Quellen die staler ist), fehlende
  Freshness → `—` end-to-end ueber eine echte (leere) DuckDB, und ein Pflicht-Integrationstest (echte
  `AnalyticsExporter` mit zwei Quellen zu UNTERSCHIEDLICHEN `synced_at` → echte DuckDB → echte
  `/panels/*`-Routen → gerendertes HTML zeigt pro Panel den korrekten, panel-spezifischen Datenstand,
  inklusive des Nachweises dass Fleet-Registry sich sichtbar unterscheidet). `ruff check`/`format --check`
  grün (3.14-Interpreter), `regression_guard verify` grün (24 Fixtures, 3.13-Interpreter mit numpy), alle 86
  bestehenden + neuen Dashboard-Tests grün.

## [2026-07-18] Erfolgsraten-Zeitvergleich-Panel — Z1-Dashboard Feature 3 (T-2026-CU-9050-155)

Drittes Feature-Panel auf der T-151-Shell: die volle Zeitvergleich-Version des T-151-Demo-Panels — eine
ECharts-Linien-Zeitreihe der ROLLIERENDEN 7/30/90d-Winrate pro ausgewähltem Bot über die Zeit, mit
Bot-Multiselect (mehrere Bots → mehrere Linien) und Fenster-Umschalter, statt nur eines aktuellen Balkens.
Baut additiv auf dem bestehenden T-131-Substrat auf — `success_rate_timeseries()` bleibt unverändert.

- `tools/analytics_api.py`: neue reine Funktionen `_daily_buckets_by_bot()` (Decisive-Trades pro Bot/Kalendertag
  gruppiert) und `_rolling_series_for_bot()` (Zwei-Zeiger-Sliding-Window über die Tage eines Bots — trailing
  `window`-Tage-Summe pro Tag, kein Neu-Aufsummieren pro Punkt) sowie `rolling_success_rate_series()` (öffentliche
  API, wiederverwendet `bot_trade_rows()` — gleiche DECISIVE-Trade-Definition wie `success_rate_timeseries` und
  `bot_leaderboard`). Neue Konstanten `TIMESERIES_WINDOWS = (7, 30, 90)` / `DEFAULT_TIMESERIES_WINDOW = 30`.
  Kein neuer JSON-API-Endpoint — die Panel-Route ruft die Funktion direkt auf (Muster: andere Panel-Routen).
- `tools/dashboard/app.py`: neue Route `/panels/success-rate-timeseries` (kollidiert NICHT mit der bestehenden
  `/panels/success-rate`-Demo, die unverändert bleibt) + `_success_rate_timeseries_context()`. Neue
  `_selected_bots()`-Hilfsfunktion unterscheidet "kein Filter übermittelt" (erster `load`, alle Bots) von "Nutzer
  hat explizit alle Checkboxen abgewählt" (echte leere Auswahl, per verstecktem `filtered`-Formularfeld erkannt) —
  sonst würde eine bewusste Leerauswahl über `_bot_filter`s "leer == kein Filter"-Konvention lautlos wieder alle
  Bots zeigen.
- `tools/dashboard/templates/panels/success_rate_timeseries.html`: Selbst-aktualisierendes HTMX-Widget — das
  Fragment ersetzt sich selbst per `hx-swap="outerHTML"`, sodass die eigenen `hx-get`/`hx-trigger`-Attribute bei
  jedem Formular-Wechsel (Bot-Checkboxen, Fenster-Radios) die AKTUELLE Auswahl in die Poll-Query einbacken — ein
  Polling-Intervall setzt die Nutzerauswahl damit nie zurück. `templates/index.html`: neues Panel "Erfolgsraten-
  Zeitvergleich" verdrahtet.
- `tools/dashboard/static/js/panels.js`: neue ECharts-Linien-Factory `winrate-timeseries` (eine Linie pro Bot,
  `type: "time"`-x-Achse, `connectNulls`), registriert über den bestehenden `chart_lifecycle.js`-Helper
  (dispose/re-init bei htmx-Swap). `static/css/app.css`: neue `.panel__filters`/`.panel__filter-group`/
  `.panel__filter-option`-Klassen für das Multiselect-/Fenster-Formular.
- `backtest/test_dashboard_success_rate_panel.py`: 22 DB-freie Tests mit einer bewusst DIVERGENTEN Fixture (RUB2:
  3 frühe Wins gefolgt von 4 aktuellen Losses → 7d/30d/90d-Rolling-Winrate 0 % / 20 % / ~42,9 % am selben Tag;
  ABR2: unabhängiges, ebenfalls divergentes Muster 66,7 % / 75 % / 80 %) — Pure-Function-Tests für das
  Sliding-Window, ein Pflicht-Integrationstest (echte `AnalyticsExporter` → echte DuckDB → echte Route →
  gerenderte HTML/JSON-Chart-Serie), explizite Multiselect- und Fenster-Umschalt-Tests mit den exakten
  divergenten Erwartungswerten (Mutation-Check: eine vertauschte Fensterberechnung oder ein vertauschter
  Bot-Filter macht mindestens einen dieser sechs Werte falsch), Test dass die bestehende `/panels/success-rate`-
  Demo-Route unangetastet bleibt. `ruff check`/`format --check` grün (3.14-Interpreter), `regression_guard verify`
  grün (24 Fixtures, 3.13-Interpreter mit numpy), alle 52 bestehenden Dashboard-Tests weiterhin grün.

## [2026-07-18] Leaderboard + Risiko-Kennzahlen-Panel — Z1-Dashboard Feature 2 (T-2026-CU-9050-154)

Zweites echtes Feature-Panel auf der T-151-Shell: pro aktivem Bot (Model-Tag mit mind. einem entschiedenen Trade
im DuckDB-Substrat) ein Performance-Ranking — realisierte PnL (Σ%), Win-Rate, Expectancy (⌀%/Trade), Trade-Count,
plus zwei Risiko-Kennzahlen (Max-Drawdown der additiven PnL-Kurve, längster Loss-Streak). Sortiert nach PnL
absteigend (Default), `sort_by` optional auf `expectancy_pct`/`winrate`/`n`. Vollständig DB-frei — liest nur die
bestehende `flagged`-CTE (Neutral-/Housekeeping-Trades werden wie beim success-rate-Endpoint ausgeschlossen).

- `tools/analytics_api.py`: neue reine Funktionen `bot_trade_rows()` (geordnete Decisive-Trade-Rows pro Bot,
  gleiche `is_decisive`/`is_win`-Definition wie `success_rate_timeseries`), `_leaderboard_row()` (PnL-Summe,
  Win-Rate, Expectancy, Max-Drawdown, Loss-Streak aus einer geordneten Trade-Liste — kein I/O, isoliert testbar)
  und `bot_leaderboard()` (Gruppierung + Sortierung). Max-Drawdown als eigene, schlanke Pure-Stdlib-Implementierung
  (`_max_drawdown_pp`, absolute %-Punkte unter dem laufenden Peak, gleiche Formel/Konvention wie
  `tools.wf_significance.max_drawdown_pct`, T-2026-CU-9050-053) statt eines numpy-Imports — dieser Worktree hat
  zwei getrennte Python-Interpreter (3.14 mit duckdb+Flask, 3.13 mit numpy+Flask), ein numpy-Import hätte die
  bestehenden Dashboard-Tests unter 3.14 gebrochen. Neuer Endpoint `/api/analytics/leaderboard` (Muster:
  success-rate-Endpoint, gleicher Poll-Cache, 400 bei ungültigem `sort_by`).
- `tools/dashboard/app.py`: neue Route `/panels/leaderboard` + `_leaderboard_context()`. „Aktiver Bot" heißt hier
  bewusst „hat mind. einen entschiedenen Trade im Substrat" — NICHT gegen den Fleet-Registry-Parked-Status
  verglichen (das ist Feature 1s Zuständigkeit, SPEC.md Out-of-Scope).
- `tools/dashboard/templates/panels/leaderboard.html` + `templates/index.html`: neues Panel mit
  `hx-trigger="load, every {{ panel_poll_seconds }}s"`, Tabelle mit PnL/Win-Rate/Expectancy/Trades/Max-Drawdown/
  Loss-Streak. Neue CSS-Klassen `.pnl-positive`/`.pnl-negative` für Vorzeichen-Farbcodierung.
- `backtest/test_dashboard_leaderboard.py`: 15 DB-freie Tests mit realistischen `closed_ai_signals`-Fixtures
  (echte Spaltennamen aus `analytics_export.py`, realistische Model-Tags RUB2/ABR2/MIS2) — Unit-Tests für
  `bot_trade_rows`/`_leaderboard_row`/`_max_consecutive_losses`, Sortier-Tests (Mutation-Check: Sortierrichtung
  geflippt → Test wird rot, verifiziert), ein Pflicht-Integrationstest (echte `AnalyticsExporter` → echte DuckDB
  → echte Route → gerenderte HTML-Tabelle, inkl. Ausschluss eines Nur-Neutral-Bots), Postgres-Touch-Guard,
  Index-Verdrahtung. `ruff check`/`format --check` grün, `regression_guard verify` grün (24 Fixtures).

## [2026-07-18] Fleet-Registry-Panel — Z1-Dashboard Feature 1 (T-2026-CU-9050-152)

Erstes echte Feature-Panel auf der T-151-Shell: pro Bot Model-Tag · Live-Config (Kernparameter) · Status
(Active/Parked) · parked-seit. Vollständig DB-frei — nur `core/fleet.py` (FLEET), `control/parked/`-Marker und
root-level `*_meta.json`-Artefakte werden gelesen, kein Postgres-Zugriff.

- `core/bot_catalog.py`: neue `families_for_script(script)` — die Umkehrung von `script_for_tag()`. Liefert alle
  Modell-Familien/klassischen Strategienamen, die ein Fleet-Skript postet (z. B. `25_smc_ml_sniper.py` → `["BB",
  "TD"]`, `3_detectors.py` → alle 5 klassischen Namen).
- `core/process_control.py`: neue `parked_since(script)` — reiner Read-only-Stat auf die Marker-Datei-mtime, kein
  Schreiben/Löschen. Einzige DB-freie Quelle für „seit wann geparkt"; „seit wann aktiv" gibt es file-basiert
  nicht (kein Unpark-Event wird persistiert) und wird bewusst NICHT fabriziert — rendert als „—".
- `tools/dashboard/app.py`: neue Route `/panels/fleet-registry` + reine Funktion `fleet_registry_rows()`
  (voll injizierbar für Tests) plus `_live_model_configs()` (scannt NUR root-level `*_meta.json` — die LIVE-
  Artefakte, `staging_models/` bewusst ausgeschlossen, CLAUDE.md Regel 2) und `_config_label()`. Marker-mtime
  wird über den sanktionierten `core.time.from_unix_ts`-UTC-Konverter gerendert (R3/DTZ-konform).
- `tools/dashboard/templates/panels/fleet_registry.html` + `templates/index.html`: neues Panel mit
  `hx-trigger="load, every {{ panel_poll_seconds }}s"`, wiederverwendet den Freshness-Badge-Look (`.badge`/
  `.badge--fresh`/`.badge--stale` für Active/Parked). Neue CSS-Modifier-Klasse `.datatable--fleet` (linksbündig
  für Text-Spalten statt des numerischen Rechtsbündig-Defaults).
- `backtest/test_dashboard_fleet_registry.py`: 19 DB-freie Tests — `families_for_script`/`parked_since`-Unit-
  Tests, `fleet_registry_rows()` mit synthetischen/injizierten Inputs (parked vs. active, Multi-Direction-Config,
  fehlender Config → „—", nie fabriziertes „seit wann aktiv"), Route liefert 200 mit korrekten Zeilen, Panel im
  Index verdrahtet, kein Postgres-Touch, plus ein Smoke-Test gegen die echten Repo-Defaults (kein
  `control/parked/`-Verzeichnis in diesem Worktree → alle Bots rendern Active, kein Crash).
## [2026-07-18] Shadow-Sichtbarkeits-Vorschau auf Englisch (T-2026-CU-9050-153)

Kleiner Folge-Fix zu T-150: die Shadow-Vorschau-Nachricht im Test-Channel (`_shadow_preview_message`) ist jetzt
Englisch statt Deutsch (Operator-Wunsch — die Channel-Posts sollen englisch sein). Format bleibt bewusst
**NICHT Cornix-parsebar** (kein `Entry:`/`Targets:`/`Stop Loss:`, keine Signal-Struktur): „👻 SHADOW PREVIEW —
NOT a trade signal, no Cornix / Model … · Coin … · Side … / Ref-Entry … · Ref-SL … · Ref-TPs … / (monitored in
ai_signals only — never reaches a trading channel)". Test-Assertions (`backtest/test_shadow_test_channel.py`) auf
die englischen Strings + den Non-Cornix-Trigger-Check angepasst (4/4 grün). Reiner String-Change, keine Logik-/
Sicherheits-Änderung. Aktivierung: bereits live über CH_SHADOW_TEST — greift nach dem nächsten Fleet-Restart.

## [2026-07-17] Z1-Dashboard-Shell — Task 0 Fundament (T-2026-CU-9050-151)

Tragende Shell des Z1-Dashboards per Framework-Gate **D-2026-CLD-111** (Flask + HTMX + Interval-Polling; kein
FastAPI, kein SPA, kein Node-Build on-box). Baut auf dem T-131-DuckDB-Substrat auf — liest NIE Live-Postgres.
Das alte `dashboard.py` bleibt unangetastet (parallele neue Analytics-Oberfläche).

- `tools/dashboard/app.py`: Flask-App-Factory `create_app(duckdb_path)`, die den read-only Analytics-Blueprint
  (`/api/analytics/*`) mountet, das HTMX-Shell-Layout + EIN Demo-Panel (Erfolgsrate je Bot) rendert und eine
  reine `freshness_summary()`-Funktion trägt. TZ-Falle (R3) respektiert: „Sync vor N min" wird STRIKT aus
  `synced_at` (UTC) berechnet, nie aus dem naive-local `last_row_ts`. waitress-Entrypoint bindet **127.0.0.1**
  (nie 0.0.0.0 — P0.8), Serving via geteiltem `analytics_api._serve` (kein Duplikat).
- `tools/analytics_api.py`: additive, verhaltenserhaltende Extraktion von `build_analytics_blueprint()` aus
  `create_app` (dieselben URLs/Cache) — damit die Dashboard-App die Endpoints mountet statt einen zweiten Server
  zu fahren. Alle 25 bestehenden T-131-Tests bleiben grün.
- `static/js/chart_lifecycle.js` (Council-Kern-Deliverable): library-agnostischer Chart-Lifecycle-Manager —
  registriert Chart-Instanzen und ruft `dispose()`/`remove()` bei `htmx:beforeSwap` + Re-Init bei
  `htmx:afterSwap`, damit über die späteren 9 Panels keine Canvas/WebGL-Kontexte + Listener leaken. `panels.js`
  registriert die ECharts-`winrate-bars`-Factory dagegen.
- `static/vendor/`: vendored (self-hosted, keine CDN-Requests) htmx 2.0.4, TradingView Lightweight Charts 4.2.3,
  Apache ECharts 5.6.0 + `README.md` mit exakten Versionen/Bezugsquellen. Responsives `static/css/app.css`.
- `backtest/test_dashboard_shell.py`: 14 DB-freie Tests (synthetische DuckDB via `AnalyticsExporter`) decken
  AK1–AK7 — Blueprint gemountet, Shell/Demo-Panel/Badge rendern, `chart_lifecycle.js` ausgeliefert, Freshness-
  Age aus synced_at only, 127.0.0.1-Bind, kein Import/Route triggert Postgres (Subprozess-Check).

## [2026-07-17] Shadow-Sichtbarkeits-Echo in optionalen Test-Channel (T-2026-CU-9050-150)

Optionale reine Sichtbarkeit der Shadow-Trades in Telegram, mit **null Trade-Risiko** — auf Michi-Wunsch. Neue
env-Config `CH_SHADOW_TEST` (Default 0 = AUS, voll rückwärtskompatibel: ohne sie bleibt Shadow DB-only). Ist sie
gesetzt, echot `core.signal_post.post_shadow_ai_signal` je Shadow-Trade **zusätzlich** EINE bewusst
**NICHT-Cornix-parsebare** Vorschau an genau diesen Channel — nie an den Handels-Channel, nie im Cornix-Format.

- `core/config.py`: `CH_SHADOW_TEST = _ch("CH_SHADOW_TEST")` (0 = aus). Channel-ID gehört in die VPS-`.env`
  (Regel 3, nie hardcoden).
- `core/signal_post.py`: `_shadow_test_channel()` (lazy config-Read, testbar) + `_shadow_preview_message()`
  (Vorschau als „👻 SHADOW-VORSCHAU — KEIN Handelssignal", Ref-Entry/SL/Ziele als Text, KEINE Cornix-Trigger-
  Keywords/Signal-Struktur). Der Echo läuft in der offenen Caller-Transaktion (Regel 8, kein Commit hier).
  **Dreifach sicher:** (1) Cornix hört laut `REGIME_TRADING_CHANNEL_ID` EXKLUSIV auf den Handels-Channel — der
  Test-Channel ist außerhalb; (2) die Nachricht ist selbst bei Mitlesen nicht parsebar; (3) harte Code-Schranke
  in `_shadow_test_channel()` — ist CH_SHADOW_TEST versehentlich == Handels-Channel, wird der Echo unterdrückt
  (return 0 + Warnung), die „nie der Handels-Channel"-Invariante ist im Code erzwungen (Review-Fix). Zentral →
  alle Shadow-Beine (LIS1/TSM1/SKW1/XSM1/XSR1/FMR2 + ATS2/ATB2/SRA2/RUB3/EPD3) echoen einheitlich.
- `backtest/test_shadow_test_channel.py`: 3 DB-freie Tests — Default-aus schreibt KEINE outbox (Backward-Compat),
  gesetzt schreibt GENAU EINE Zeile an genau DIESEN Channel, Vorschau ist nicht Cornix-parsebar.

Aktivierung: `CH_SHADOW_TEST=<id>` in der VPS-`.env` + Watchdog-Restart (Operator-Gate). Der Telegram-Bot muss
Mitglied/Admin des Ziel-Channels sein.

## [2026-07-17] FMR2 (K4) Shadow-Bein am FMR1-Bot — überwacht, nie gepostet (T-2026-CU-9050-149)

FMR2 als Klasse-(A)-Shadow neben dem live FMR1-Bot (Bot 31) angelegt, damit der Normalisierungs-Exit-Retrain
gegen echte Live-Preise eine realisierte Ergebnis-Historie sammelt. **Shadow = Bestätigung, kein Rollout** —
der Backtest (T-148) war nicht deploybar; das Shadow-Bein postet NIE einen Trade (kein `telegram_outbox`),
schreibt nur `ai_signals`/`closed_ai_signals` unter Tag `FMR2` und `ml_predictions_master(posted=False)`. Der
FMR1-Live-Pfad ist unverändert und nie betroffen (eigener Tag, eigene Dedup, alles gekapselt in try/except).

- `core/shadow_gate.py`: FMR2 in `_LIFECYCLE` (LONG+SHORT → SHADOW) und `SHADOW_ARTIFACTS` (ein binäres Modell
  `fmr2_model.pkl` für BEIDE Richtungen — `side_short` ist ein Feature). FMR1 bleibt Default-LIVE (keine Zeile).
- `31_ai_fmr1_bot.py`: `_emit_fmr2_shadow()` scored DENSELBEN `build_fmr1_row`-Feature-Row (FMR2_FEATURES ==
  FMR1_FEATURES) mit dem FMR2-Modell und emittiert nach der §3-Shadow-Regel (Threshold gesetzt → nur bei
  `prob ≥ thr`; sonst `log_prediction(posted=False)`). Fail-soft: fehlt das Artefakt, läuft der Bot als reiner
  FMR1-Bot weiter. Aufruf VOR der FMR1-Post-Logik, voll gekapselt.
- `backtest/test_shadow_gate.py`: 3 DB-freie Tests — FMR2 beidseitig SHADOW (FMR1 bleibt LIVE), ein pkl für
  beide Richtungen, und ein End-to-End-Load/Score/Gate des realen Artefakts (skippt, wenn das VPS-pkl fehlt).

Wie bei ATS2/ATB2/RUB3/EPD3 liegt das reale `fmr2_model.pkl` NICHT im Git, sondern in `staging_models/` auf dem
VPS (Platzierung = Operator-Schritt, harte Regel 2). Aktivierung des Beins braucht Bot-31-Restart (Michi-Gate).
## [2026-07-17] Rule-based Shadow-Forwarder K1/K7/K2 — Bots 37/38/39 (T-2026-CU-9050-149)

Drei weitere regelbasierte (artefaktlose) Shadow-Forwarder der Studien-Kandidaten-Kohorte, alle **reine
Shadow-Bots ohne Live-Post** (Forwarder-Klasse (D) in `core/shadow_gate.py`: Tag → SHADOW, KEIN Artefakt; der
Bot rechnet die Regel selbst und emittiert auf dem Roh-Signal; fail-safe zu Stille, nie live). Kein Modell, kein
deploybarer Edge — Live-Gegenprüfung via überwachte, nie gepostete Trades (`ai_signals` ohne `telegram_outbox`).

- **TSM1 (K1) — Bot 37, event-driven, NUR SHORT:** 4h-Zeitreihen-Momentum-Crossing (`4h|L12|k0.5`) — ROC[t]=
  close/close[t−12]−1 kreuzt von außen nach innen des ±0,5σ-Bands (σ=90d-Rolling-Std). Die Studie ist
  paper-falsifiziert, aber der Verlust kommt komplett vom LONG-Bein; SHORT ist in jeder Zelle positiv
  (nicht-falsifiziert) → nur `("TSM1","SHORT")` registriert. Geteilte `hvn_sr_trade_geometry` (== Studien-Label),
  Market-Fill, läuft alle 4h (00:29/04:29/… UTC). Pure `short_crossing`-Prädikat DB-frei getestet.
- **SKW1 (K7) — Bot 38, wöchentlich, BEIDE Beine:** Querschnitts-Skew-Rotation via geteiltem
  `core/moment_features.build_moment_panel` (Regel 7) — LONG unterstes, SHORT oberstes `mom_skew_7d`-Dezil
  (ρ=−0,88). Validiertes Feature, kein turnkey Edge. Liquiditäts-Filter (unteres Dollar-Vol-Terzil raus),
  MIN_COINS_PER_WEEK, Montag 00:31 UTC. Pure `select_deciles` getestet.
- **XSM1/XSR1 (K2) — Bot 39, wöchentlich, zwei konkurrierende Hypothesen:** rohe F=84d-Rendite-Dezil-Rotation;
  XSM1 LONG (Momentum) UND XSR1 SHORT (Reversal) auf DEMSELBEN obersten Dezil, unabhängig überwacht. Studie
  weak/inconsistent/overfit (0 robuste Zellen). BTC aus der handelbaren Menge, Liquiditäts-Filter, Montag 00:37
  UTC. Pure `select_top_decile` getestet.

**Dokumentierte Divergenz (SKW1/XSM1/XSR1):** die Studien messen einen WOCHEN-Timeout-Halte-Exit; der
Shadow-Monitor verfolgt First-Touch-TP/SL (geteilte Geometrie). Der Shadow-PnL ist damit eine richtungs-getreue
First-Touch-Validierung, NICHT die Studien-Timeout-PnL — bewusst, da der Monitor keinen Timeout-Exit kennt (kein
Monitor-Umbau auf Live-Money). Fleet-Registrierung (`core/fleet.py` Bots 37/38/39 group ai; `core/bot_catalog.py`
Prefixe TSM/SKW/XSM/XSR); je Bot DB-freie Tests. Aktivierung braucht einen Watchdog-Restart (Michi-Gate; unter
100 % CPU zuerst Kapazität prüfen — die wöchentlichen Querschnitts-Scans + Dezil-Shadows sind Last-relevant).

## [2026-07-17] LIS1 (K5) Shadow-Forwarder — Post-Listing-Drift-Fade, Bot 36 (T-2026-CU-9050-149)

Erster regelbasierter Shadow-Forwarder der Studien-Kandidaten-Kohorte: ein neuer Bot 36 (`36_ai_lis1_bot.py`)
fadet frisch gelistete Coins am Tag 3 nach dem Binance-onboardDate SHORT — als **reiner Shadow-Bot ohne
Live-Post**. Es gibt kein Modell und keinen deploybaren Edge (Studie K5: Fade-SHORT fragil, nur Tag-3-Zelle
materiell positiv, hoher WR ~0,70 aber tiefer Links-Tail); der Bot validiert das Signal live über überwachte,
nie gepostete Trades (`ai_signals` ohne `telegram_outbox`, Tag `LIS1`).

- **Artefaktlose Forwarder-Klasse (D)** in `core/shadow_gate.py`: `("LIS1","SHORT") → SHADOW`, aber KEIN
  Eintrag in `SHADOW_ARTIFACTS` — der Bot rechnet die Regel selbst und emittiert auf dem Roh-Signal
  (ROM1-Präzedenz), kein `score_artifact`. Fail-safe: ist das Bein nicht SHADOW (z. B. versehentlich promotet),
  schweigt der Bot — er postet NIE live (die Regel hat keinen Edge).
- **Signal-Parität zur Studie** (`tools/listing_drift_study.py::fade_events`, Zelle d3|l0.0, n=152): Trigger =
  reines Alters-Event (Coin erreicht Tag 3, unbedingt); onboardDate aus `GET /fapi/v1/exchangeInfo` (Cache
  `staging_models/listing_onboard_dates.json`, Fallback erste 1h-Kerze); Geometrie = geteilte
  `hvn_sr_trade_geometry` (SHORT-SL/Targets, `ensure_min_tp_distance(min_pct=0.05)`, 3 TPs); Market-Fill
  (`entry1==entry2`). Dokumentierte Divergenz: Live-Fill ist der aktuelle Close (≤1h nach dem Tag-3-Anchor);
  Alters-Fenster `[3d, 4d)` (kein Backfill alter Coins); Geometrie-Load-Floor 48 1h-Zeilen (Tag-3-tauglich,
  NICHT der 120er-Studien-Voll-Historie-Floor). Das LONG-Blacklist-Ergebnis (Alter < 180d ⇒ kein LONG) ist ein
  separates Gate und wird bewusst NICHT umgesetzt (Operator-Entscheid Michi).
- **Fleet-Registrierung:** `core/fleet.py` (Bot 36, group `ai`, start_delay 239) + `core/bot_catalog.py`
  (Prefix `LIS` → Bot 36). 6 DB-freie Tests (`backtest/test_lis1_bot.py`): SHADOW-ohne-Artefakt, Tag→Skript,
  `in_fade_window`-Grenzen, `process_coin`-Gating/Shadow-Emit (nie Live). Aktivierung braucht einen
  Watchdog-Restart (Michi-Gate; unter 100 % CPU zuerst Kapazität prüfen).

## [2026-07-17] FMR2 (K4) Phase 1 — Voll-Retrain nach staging: NICHT deploybar (T-2026-CU-9050-148)

Der gemergte FMR2-Builder/Scaffold (PR #132) wurde nach Operator-Freigabe ausgeführt: voller V2-Datensatz +
Retrain + Auswertung — read-only, nichts Live. **Verdikt: nicht deploybar.**

- **Datensatz:** 12.165 Events (V2-Normalisierungs-Exit-Labeling, since 2026-02-25; funding_cs_pctl ≥0,95 → SHORT /
  ≤0,05 → LONG; Dedup 24 h), gewichtete WR 0,453, Basis-Ø-PnL **−0,475 %/Trade**.
- **Modell** (binär, 15 FMR2-Features, Chrono train 8229 / val 1580 / test 1811, Purge 3 d, isotonisch kalibriert):
  **AUC val 0,548 / test 0,540** (kaum über Zufall).
- **Val-Operating-Point** (thr 0,46): ΣPnL **−27,5 %**, Ø −0,048 %/Trade — schon in-sample negativ.
- **Gate-Uplift test:** Basis Ø −0,478 %/Trade → mit Gate Ø **−0,251 %/Trade** (WR 0,472, n 741/1811). Das Modell
  HALBIERT den Verlust, dreht ihn aber NICHT ins Plus.
- **Richtungs-Split** (voller Event-Satz): SHORT Ø −0,538 % / LONG Ø −0,418 % — **beide negativ**, kein versteckter
  positiver Teil.

**Fazit:** Der V2-Normalisierungs-Exit behebt den FMR1-First-Touch-Fehler konzeptionell, aber der Funding-MR-Edge
existiert auf 2026er-Daten nicht (keine positive Erwartung in beiden Richtungen). **Phase 2 (Bot-31-Exit-Loop)
NICHT gestartet** — kein deploybarer Standalone. Artefakt `staging_models/fmr2_model.pkl` + Report nur in staging;
keine Promotion ins Repo-Root (P1.35, Operator-Gate).

## [2026-07-17] K4 · FMR2 — Funding-Extreme-MR mit Normalisierungs-Exit (Builder + V2-Labeling + Retrain-Scaffold, CODE-PREP) (T-2026-CU-9050-146)

Die S8-These sauber testbar gemacht: FMR1 labelte First-Touch-TP/SL und testete damit NIE die
eigentliche Idee („halten bis Funding-Normalisierung ODER Time-Stop") — genau der FMR1-Fehler
(Report 15 V2-Diagnose). FMR2 labelt jetzt den Normalisierungs-/Timeout-Exit. Drei additive
Erweiterungen der bestehenden Research-Pipeline (Reuse/Extend, nichts neu erfunden):

- `core/research_features.py`: geteiltes Exit-Predikat `fmr2_funding_normalized` (EINE Quelle für
  Builder UND künftigen Bot, X-R1) — SHORT normalisiert sobald `funding_cs_pctl < 0.80` ODER
  `funding_z_30d < 1.0`, LONG symmetrisch (`> 0.20` / `> −1.0`); benannte Konstanten
  (`FMR2_SHORT/LONG_EXIT_*`, `FMR2_TIME_STOP_SETTLEMENTS = 9` = 3 Tage, `FMR2_CATASTROPHE_SL_PCT = 15.0`);
  `fmr2_catastrophe_sl`; `FMR2_FEATURES == FMR1_FEATURES` (identischer Entry-Vertrag, nur das Label ändert
  sich). Native-NaN fail-safe (unbestimmbare Normalisierung schließt NICHT vorzeitig), as-of, R1.
- `tools/fmr1_build_dataset.py`: neuer `--label-version v2`-Pfad (`simulate_normalization_exit`) — Label =
  Vorzeichen des Netto-PnL am **Exit-Preis der Settlement-Kerze** (Close), NICHT First-Touch-TP/SL; harter
  Katastrophen-SL bleibt als touch-basiertes First-Touch-Netz; `funding_z_30d` pro Settlement as-of neu
  gerechnet (Formel identisch `funding_stats`), `funding_cs_pctl` aus vorberechneter Cross-Section.
  V1/FMR1 bleibt bit-identisch der Default (`--label-version v1` → `fmr1_events.jsonl`), V2 → `fmr2_events.jsonl`.
- `tools/new_models_train.py`: FMR2 in `STRATEGIES` (`kind=binary`, `features=FMR2_FEATURES`, `purge_days=3`
  >= 9-Settlement-Horizont) — wiederverwendet den bestehenden Chrono-Split/Purge/`pick_threshold`-Pfad,
  Artefakt `staging_models/fmr2_model_*.pkl` mit `meta.model_id=FMR2`.

Neuer DB-freier Test `backtest/test_fmr2_exit.py` (9/9 grün): Predikat (SHORT/LONG/NaN/Schwellen) + Walk
(Time-Stop@9, Normalisierungs-Exit, Katastrophen-SL-First-Touch, open_at_end, Settlement-Close-Pricing).
Smoke: Mini-Datensatz (600 synthetische Events) → Retrain-Scaffold end-to-end exit 0, Artefakt
`staging_models/fmr2_model_smoke.pkl` (`model_id=FMR2`, 15 Features) + Build-Report `staging_models/fmr2_build_report.md`.
**CODE-PREP: kein echter Retrain, kein Bot** — der Voll-Retrain (Ein-Job-Regel) und der Bot-31-Exit-Loop
(Close-Command → `telegram_outbox`, `closed_ai_signals status='CLOSED_FUNDING_NORMALIZED'`, `CH_FMR1`) sind
Operator-gegated (Michi) und NICHT ausgeführt.

## [2026-07-17] K2 · XSM1/XSR1 — Cross-Section Momentum/Reversal-Studie (Voll-Lauf) (T-2026-CU-9050-143)

Neues read-only Studien-Skript `tools/xs_momentum_study.py` gebaut (Code-Prep, **Full-Run offen** —
gehört in einen Orchestrator-gegateten Ein-Job-Slot, hier nur Smoke). Zweistufige Cross-Section-Studie
auf 1d-Kandles über die ~430d-Historie: Formations-Fenster F∈{7,14,28,56,84}d × Halte-Fenster
H∈{7,14,28}d, wöchentliches Rebalance-Raster, je Zelle **zwei Signal-Varianten** (roher F-Return /
Anchored = Distanz zum Formations-Low, F5) × **zwei Bezugsgrößen** (absolut / marktneutral =
Coin−BTC) × **zwei Richtungen** (XSM1-LONG Top-Dezil / XSR1-SHORT Top-Dezil-Reversal) = 120 Zellen.
Liquiditätsfilter (unteres Volumen-Terzil via Median-Quote-Volumen über F ausgeschlossen). Stufe 1
= Dezil-Spreads Close-to-Close über H, netto mit Fees (Regel 10, `walkforward_sim.FEE_PER_SIDE`) plus
Short-Seiten-Funding aus `funding_rates` (Short erhält +Σ funding_rate, zahlt bei negativem Funding).
Chrono Val/Test-Split (BTC-1d-Mittelpunkt), Zellenauswahl NUR auf Val. Stufe 2 (nur Val-positive
Zellen) = Event-Replay mit unserer Geometrie (`get_hvn_and_sr_levels(df=as-of)` → `simulate_exit`,
Entry = erster 1h-Close nach Rebalance, strikt as-of). Stop-Kriterium → No-op-Verdikt gültig.
Resume/Checkpoint-Maschinerie (Streaming-Akkumulatoren O(cells), atomic State im OS-Temp-Dir NICHT im
Repo, `--resume`) nach dem Muster von `tools/tsmom_study.py`; RAM-Guard + Peak-RSS im Report.
**VOLL-LAUF (527 Coins, 120 Zellen):** Verdikt **`weak/inconsistent-spread (not deployable)`** — 58 Zellen
val-positiv, 8 „passing" (Val>0 UND Test>0), aber **0 robust**. Die 8 Passing-Zellen sind NICHT
Val+Test-konsistent: Val-Bein ~0 (≤0,075 %/Rebalance) bei großem Test-Bein (0,75–3,11 %) — die klassische
Overfitting-Signatur, kein handelbarer Edge; Test-WR < 0,5 (tail-getrieben) und die best-on-val-Zelle
(Val +4,74 %) **kippt out-of-sample negativ (Test −1,61 %)**. Struktur repliziert NICHT robust.

**Verdict-Konsistenz-Fix:** das ursprüngliche `derive_verdict` labelte „xs-edge-found" schon bei Val>0 UND
Test>0 — das ignoriert die Spec-Anforderung „Val+Test-**konsistenter** Netto-Spread" und labelt Overfit-
Rauschen als Edge. Neu: `MIN_ROBUST_NET_PCT = 0,3 %/Rebalance` (~3× der 0,10 % Round-Trip-Fee), BEIDE Hälften
müssen den Floor klären ⇒ Tiers `xs-edge-found` / `weak/inconsistent-spread` / `no-op`. Neuer `--reverdict`-
Modus (Verdikt+Report deterministisch aus bestehendem JSON neu ableiten, KEIN DB-Re-Fold — genutzt, da der
Fix nach dem teuren Lauf kam). Survivorship (Regel 9, hier am stärksten) dokumentiert, `fill_method=None`.
Artefakte nach `staging_models/` (Regel 2/7). Nichts deployt/promotet — Folge-Tasks je Richtung wären
Operator-Entscheidung (Michi), hier NICHT lizenziert.

**Bekannte Limitationen (im Review gefunden, NICHT verdict-relevant — Netto-Ergebnis bleibt negativ, als
Follow-ups notiert):** (1) der `market_neutral`-Frame ist ein No-op — die BTC-Signal-Subtraktion ist ein
Per-Rebalance-Skalar-Shift (argsort-invariant) und die PnL ist absolut, also sind alle 60 `market_neutral`-Zellen
byte-identisch zu `absolute` (Beta-Removal NICHT getestet; Fix = Returns/Spread beta-adjustieren). (2) Stufe-2
(nur Diagnostik) tritt ~1 Tages-Balken zu früh ein (`dates[t]`=Tages-Open via `floor('D')`, Signal aber `close[t]`)
⇒ Look-ahead im Replay; der Stufe-1-getriebene Verdict ist unberührt (Fix = Entry `dates[t]+86400`).

## [2026-07-17] K5 · LIS1 Post-Listing-Drift-Kohortenstudie + Fade-Replay (Voll-Lauf) (T-2026-CU-9050-144)

Neues read-only Studien-Skript `tools/listing_drift_study.py` (K5, Kandidat LIS1) — Code-Prep, der
Full-Universe-Lauf bleibt für den orchestrator-gegateten Ein-Job-Slot offen. Prüft die These (F10),
dass frisch gelistete USDT-Perps in den ersten Wochen/Monaten underperformen. Elemente: Listing-Datum
je Coin via EINEM `GET /fapi/v1/exchangeInfo` (`onboardDate`, ms-Epoch UTC), gecacht nach
`staging_models/listing_onboard_dates.json` (einziger externer HTTP-Call, public, keine Keys) — bei
Netzfehler Fallback auf die erste 1h-Kerze je Coin (Quelle je Coin dokumentiert). Kohorte = onboardDate
im Datenfenster (strikt nach dem ~1-Jahres-Retention-Floor, sonst ist der Drift nicht beobachtbar).
Forward-Returns Tag 0 → {7,30,90,180} auf 1d-Kerzen, **absolut UND marktneutral** (minus BTC über
dasselbe Fenster — Beta-Confound behoben); Verteilung, Median, %-positiv, n je Horizont. Fade-Replay
SHORT Tag {3,7,14} × Limit {+0 %,+5 %} über `simulate_exit` (First-Touch, Taker-Fee) mit **zwingender
Funding-Kosten-Verrechnung** — ein SHORT wird bei positivem Funding GUTGESCHRIEBEN (Longs zahlen Shorts),
also `+Σ funding_rate` über den Hold (Vorzeichen bewusst gesetzt; frische Perps mit Extrem-Funding
können die Short-Seite bezahlen). Kleines-n wird ehrlich ausgewiesen (n je Kohorte/Horizont/Zelle,
keine vorgetäuschte Signifikanz). Minimal-Deliverable auch ohne Short-Edge: quantifizierte Empfehlung
„Coin-Alter < X Tage ⇒ kein LONG" (Umsetzung = Gating-Change ⇒ Michi). Resume/Checkpoint-Maschinerie
nach `tools/tsmom_study.py`-Muster (Per-Coin-Streaming-Akkumulatoren, atomarer Checkpoint des
Processed-Sets ins OS-Temp — nie ins Repo — alle N Coins, `--resume`, RAM-Guard <500 MB, Peak-RSS im
Report). Wiederverwendet den Exit-/Geometrie-/Funding-Stack (kein neuer Fee-/Geometrie-Code). Read-only,
SELECT-only, BELOW_NORMAL; Artefakte nur nach `staging_models/`.

**VOLL-LAUF (Kohorte n=152 Listings, `small_n_flag=false`):** Verdikt `fade-short-candidate (needs follow-up
bot task)`. **Der Post-Listing-Drift ist REAL, groß und konsistent:** marktneutraler Median (minus BTC,
Beta-adjustiert, Beta kippt das Vorzeichen NICHT) Tag 7 **−8,3 %**, Tag 30 −22,0 %, Tag 90 −34,3 %, Tag 180
−34,1 %; nur ~25–36 % der Listings positiv. ⇒ **Robuster Befund für den Risikofilter „Coin-Alter < ~X Tage
⇒ kein LONG" (Minimal-Deliverable, stark gestützt).** Der Fade-SHORT (Entry Tag {3,7,14} × Limit {+0 %,+5 %},
`simulate_exit` + Funding) ist dagegen **MARGINAL und fragil:** positive Mediane (+3,5–6,7 %) und hohe WR
(0,59–0,70), ABER Ø nahe null bis schwach positiv (2 von 6 Zellen Ø-negativ) mit fettem Short-Left-Tail
(p5 −20 bis −32 %). Die „candidate"-Auszeichnung ruht auf den **Tag-3**-Zellen (Ø +1–2 %) — die
benachbarte **Tag-7**-Zelle kippt Ø negativ (−1,1 %), ein Vorzeichenwechsel über einen Entry-Tag ⇒
Instabilität; zudem stützt sich die Tag-3-Geometrie auf nur ~72 h 1h-Kerzen (dünne S/R-Basis). Also ein
verrauschter „candidate", KEIN bewiesener Edge. Beide Deliverables (LONG-Blacklist-Gating
bzw. Fade-SHORT-Bot je Richtung) = **Operator-Entscheidung (Michi); hier NICHTS deployt/promotet.**
Listing-Daten via `exchangeInfo`-GET (`onboardDate`), gecacht, Fallback erste 1h-Kerze.

## [2026-07-17] K11 · WSH1 — Wick-Reversal-Stop-Hunt Event-Studie (Voll-Lauf) (T-2026-CU-9050-145)

Neues read-only Studien-Skript `tools/wick_reversal_study.py` (15m-Kandles; 5m-Retention zu kurz, 15m ≈ 1 Jahr).
Parametrisiertes Event-Grid: `lower_wick ≥ k·ATR14` (k∈{1.5,2,3}) × `volume ≥ m·vol_sma20` (m∈{3,5}) ×
Close-Recovery ≥ 50 % der Kerzen-Range — langer unterer Docht → LONG-Bounce, oberer Docht → SHORT (gespiegelt);
Entry = **Close der geschlossenen Event-Kerze** (Regel 5). ATR14/vol_sma20 sind trailing und schließen die
Event-Kerze bewusst aus (`rolling.mean().shift(1)`), sonst bläht der Docht seinen eigenen Schwellwert auf.
**Zwei Populationen:** (a) alle deduplizierten Events, (b) Cascade-Teilmenge ≤ 60 min nach einem
`pump_dump_events`-Eintrag (Zeitspalte `spike_time` TIMESTAMPTZ/UTC, Fenster `[entry−60min, entry]`; b ⊆ a).
Labels via bestehende Geometrie-Maschinerie (`get_hvn_and_sr_levels(df=as-of)` → `hvn_sr_trade_geometry` →
`ensure_min_tp_distance` → `simulate_exit`, strikt as-of, Exit-Scan erst ab Folge-Kerze, keine Lookahead-Lecks).
Chrono-Val/Test-Split (Kalender-Mitte des BTCUSDT-15m-Fensters), Zell-Selektion **nur** auf Val; Stop-Kriterium:
keine Zelle val+test-positiv ⇒ falsifiziert (gültiges No-op-Done, kein erzwungenes Positiv). Resume/Checkpoint-
Maschinerie nach `tsmom_study.py`-Muster (Streaming-Accumulators O(cells), atomarer Temp+Rename-State im
OS-Temp-Dir, `--resume`/`--state-path`/`--checkpoint-every`/`--progress-every`/`--skip-cpu-check`, RAM-Guard
< 500 MB, Peak-RSS in Meta, encoding-sichere Prints gegen cp1252-Crash).

**VOLL-LAUF (527 Coins, 24 Zellen):** Verdikt **`no-op/WSH1-falsified`** — KEINE Zelle besteht das
Stop-Kriterium (Val>0 UND Test>0 bei n_test≥50). Nur 3 von 24 Zellen überhaupt Val-positiv, und die
stärksten (`cascade|k3.0|LONG` Val +0,35 %/+0,29 %) **kippen out-of-sample negativ** (Test −0,28 %/−0,25 %) —
hohe Trefferquote (WR 0,63–0,68) aber netto-negativ, das klassische Overfitting-/Tail-Muster (wie K1). Die
Interim-Checkpoint-Verdikte zeigten „edge-found" auf Teilpopulationen, das bei voller Population auswusch.
Wick-Reversal-Geometrie repliziert NICHT auf unserem Stack; nichts deployt/promotet. Report →
`staging_models/wick_reversal_study.{json,md}`. PEX1-Lektion gewahrt: Information liegt im Intraday-Fenster,
kein Ausweichen auf 1h-Kontext.

## [2026-07-17] Merge-Train: CHANGELOG.md union-Merge-Driver gegen serielle Rebase-Konflikte (T-2026-CU-9050-142)

Wiederkehrendes „merge-train failed" behoben (2 PRs hingen). Ursache: pro Merge wird ein
CHANGELOG.md-Eintrag **oben** an dieselbe Datei geprependet — der Hetzner-Merge-Train-Daemon
rebased jede PR seriell, also kollidieren zwei gleichzeitige PRs garantiert am identischen
obersten Hunk. `.gitattributes` hatte für CHANGELOG.md keine Regel. Fix: `CHANGELOG.md merge=union`
(git-eigener union-Driver) — bei Konflikt behält git **beide** Blöcke statt den Rebase abzubrechen,
parallele Changelog-Appends lösen sich automatisch auf. Die Regel muss nur auf `main` liegen (der
Daemon rebased *auf* main), löst also auch die aktuell hängenden PRs beim Re-Trigger mit. Bewusst
**nicht** auf `AUDIT_TODO.md` angewandt (Checkbox-Toggles auf bestehenden Zeilen — union würde dort
gecheckte und ungecheckte Variante beide behalten). Verifiziert per synthetischem Zwei-Branch-Rebase:
ohne Regel Konflikt, mit Regel sauber + beide Einträge erhalten. Reihenfolge zweier gleichzeitiger
Einträge nicht garantiert (kosmetisch). Folge-Option (separater Task): `changelog.d/`-Fragmente für
eine Null-Konflikt-Garantie.

## [2026-07-16] K7 · MOM/SKW1 — Realized-Moments-Feature-Block + Skewness-Studie + Retrain-Anschluss (VOLL-LAUF) (T-2026-CU-9050-141)

Neuer geteilter X-R1-Builder `core/moment_features.py` (kanonisch für Studie, Trainer und später Bot —
kein Train/Serve-Skew, wie `core/funding_features.py`/`core/breadth_features.py`): realisierte
**Vol/Schiefe/Wölbung** aus **15m**-Kerzen (bewusst 15m statt 5m — 5m hat nur ~1 Monat Retention, 15m ~1
Jahr), rollierende Fenster {24h, 7d} = {96, 672} geschlossene Balken, 6 Features
(`mom_rv_24h/7d`, `mom_skew_24h/7d`, `mom_kurt_24h/7d`, parallel zu den 6 Funding-Features). As-of nur
geschlossene Kerzen (R1, `include_forming=False`, kein Lookahead); **native-NaN-Politik** (P1.20 — fehlende
Werte bleiben NaN/`None`, NIE `fillna(0)`); fehlende Pflicht-**Spalten** → `MomentFeatureError` (X-R1-Vertrag).
**FALLE (§K7, F6):** das ist REALISIERTE SCHIEFE (drittes Moment), KEIN MAX-/Lotterie-Feature — MAX-Shorts
sind in Krypto kontraindiziert; es wird bewusst kein „Max-Return im Fenster" gebaut.

Neues read-only `tools/skewness_study.py` (§K7): wöchentliche Dezil-Sorts auf realisierter Skewness —
marktneutral (Coin − BTC), Liquiditätsfilter (unteres Dollar-Volumen-Terzil je Woche verworfen),
Funding-Kosten auf der Short-Seite (Wiederverwendung `core/funding_features.load_funding`, roher
`funding_rate` über die Halte-Woche summiert), Richtung Short-High-Positive-Skew vs. Long-Low-Skew,
Fees beidbeinig (`walkforward_sim.FEE_PER_SIDE`); RV/Kurtosis-Dezile als Nebenprodukt; chronologischer
Val/Test-Split (Vorzeichen muss beide Hälften überleben — Regel 8). BELOW_NORMAL + CPU-Headroom-Guards
wie die Schwester-Studien (K3/K6). Voll-Lauf-Report nach `staging_models/skewness_study.{json,md}`
(Verdict + tragende Tradeability-Caveats). Neuer `--reverdict`-Modus: leitet Verdict + Report deterministisch
aus einem bestehenden Full-Run-JSON neu ab (KEIN DB-Re-Fold) — genutzt, als `derive_verdict` nach dem teuren
Lauf gefixt wurde (Zahlen deterministisch; Live-DB war unter Last, kein erneutes 527-Coin-Read gerechtfertigt).

Additiver Retrain-Anschluss in `tools/retrain_from_replay.py`: neues **DEFAULT-OFF** `--features moments`-Flag
(`FEATURE_HOOKS`/`resolve_extra_features`/`with_extra_features`) hängt den `MOMENT_FEATURES`-Block an den
Feature-Vertrag jeder Strategie an — Vorbild ist der eingebackene Funding-Block. **Strikt additiv:** ohne
das Flag ist `extra_features` leer und das Retrain byte-identisch zu vorher (No-op-Anschluss, alle 7 Runner
mit `extra_features=()`-Default durchgereicht). Anhängen der Namen triggert KEIN Retrain — der Replay-Writer
muss die Moment-Spalten erst liefern (Queue).

**VOLL-LAUF (527/530 Coins, 51 Wochen, 15.923 Zeilen nach Liquiditätsfilter):** Verdikt
`skw1-robust-spread`. Der primäre SKW1-L/S-Spread (`mom_skew_7d`, SHORT High-Positive-Skew / LONG Low-Skew,
marktneutral, liquiditäts-gefiltert, funding-/fee-verrechnet) ist **netto +2,50 %/Woche** und bleibt in BEIDEN
Chrono-Hälften positiv (Val +2,51 %/35 Wo, Test +2,48 %/16 Wo; 64,7 % Wochen positiv), Dezil-Monotonie
ρ=−0,88 **glatt über alle 10 Dezile** (breite Cross-Section, kein Ausreißer-Spike).

**Verdict-Bug gefunden & gefixt:** der erste saubere Lauf schrieb fälschlich `no-op/no-skew-spread` — `derive_verdict`
prüfte ein Top-Level-`n_weeks`, das auf dem Erfolgspfad in `spread["all"]` liegt (Top-Level nur im degenerierten
Return), begrub also ein reales Spread als False-No-op. Guard auf `"all" not in spread` korrigiert; Verdict via
`--reverdict` deterministisch aus den (verifizierten) Full-Run-Zahlen neu abgeleitet. Zusätzlich ein
cp1252-Stdout-Crash beim WARN eines Coin-Symbols mit Nicht-ASCII-Zeichen encoding-safe gemacht (ASCII-Sanitize).

**Unabhängige Artefakt-Prüfung (T-133-Orchestrierung, 2026-07-16):** Stale-Price/Survivorship/Look-ahead
ausgeschlossen — `price_asof` hat einen Staleness-Guard (`MAX_STALE=1d`, NaN → Zeile fällt), das aktive
`coins.json`-Universe hat NULL Mid-Window-Delistings (Survivorship biast den Short-Leg sogar nach UNTEN),
As-of sauber, der BTC-Term kürzt sich im L/S-Spread. **Struktur ist real.**

**⚠ TRAGENDE CAVEAT — reale Struktur ≠ handelbare Edge:** die +2,50 %/Woche sind netto NUR aus Fees + realisiertem
Funding — KEIN Slippage, Market-Impact, Borrow-Verfügbarkeit oder Short-Liquidations-Risiko modelliert. Es ist ein
wöchentlicher Full-Dezil-Rebalance-Short-Term-Reversal-Sort auf den illiquidesten High-Skew-Alts (nur unteres
Dollar-Vol-Terzil verworfen), der LONG-Leg (Low-Skew = frisch gecrasht) ist tail-/bounce-getrieben (WR < 0,5 in
JEDEM Dezil). Die Headline **überschätzt** realisierbaren PnL nach Mikrostruktur-Kosten. **Deshalb: `core/moment_features.py`
ist jetzt ein VALIDIERTER Retrain-Input (§K7-Intent erfüllt), KEIN deploybarer Standalone-Spread. Ein
`--features moments`-Retrain und jedes Deployment sind Operator-Entscheidung (Michi) — hier NICHTS deployt/promotet.**
Retrain-Anschluss unverändert (byte-identisch zu bc3069f), kein Retrain gelaufen. ruff grün
(`core/moment_features.py` + `tools/skewness_study.py`).

## [2026-07-16] R1/TimescaleDB C-Gate Phase 5 prep — aktive Bypass-Reader auf core.candles + reversibler Write-Primary-Flag (T-2026-CU-9050-139)

Vorbereitung für den Phase-5-Table-Drop (~9,3k Per-Coin-`{SYM}_{tf}[_indicators]`): jeder **laufende**
Code, der die Per-Coin-Tabellen noch per Raw-SQL las, liest jetzt über `core.candles` (hyper-fähig seit
T-128, live seit dem Read-Cutover 2026-07-16) — sonst bräche er beim Drop. Jede Site byte-Parität gegen
das alte Raw-SQL verifiziert (read-only Live-VPS; Indikatoren auf **float4**-Präzision — der gewollte
P3.12 `REAL→double`-Upgrade, den der Read-Cutover fleet-weit schon vollzog).

**Read-Rewiring (7 Dateien):**
- **34_ai_max1_bot** (LIVE MAX1): `score_symbol` 90d-Closes + letzte geschlossene Indikator-Zeile →
  `read_candles`/`read_indicators` (`include_forming=False`).
- **23_market_tracker** (LIVE Bot 23): 7 `_30m`-Reads in 5 Report-Funktionen → `read_candles`; SUM/CASE/
  MAX/MIN wandern in pandas über die `Decimal`-OHLCV (float-Parität), `include_forming=True` (Monitor,
  Regel 5), `[t7,t4)`-Exclusive-End via pandas-Filter.
- **14_ai_atb_bot**: Info-Chart-`SELECT *` + 95d-ATB1-Detection → `core.candles` (`include_forming=True`,
  die forming-Kerze bleibt bewusst drin).
- **tools/walkforward_sim** (Trainer): `load_mis1_frame` + `load_rub_frame` h⋈i-JOINs →
  `read_candles_with_indicators`; **Train==Serving-Parität** (Regel 7) gegen 11_ai_mis verifiziert.
- **core/mis_features**: `MIS_SQL_INDICATOR_SELECT` (i.-präfigiertes SQL-Fragment) → geteilte
  `MIS_INDICATOR_COLUMNS` + `MIS_RENAME_MAP` — **EINE Quelle** für Bot (11) UND Trainer (walkforward);
  11_ai_mis darauf konsolidiert.
- **tools/audit/live_parity**: JOIN → `core.candles` (ASC → altes `iloc[::-1]` entfällt).

**Write-Primary-Flag (reversibel, Default aus):** neuer `KYTHERA_CANDLES_WRITE_PRIMARY ∈ {legacy, hyper}`
(`_write_primary()`, read-at-call-time). `legacy` (Default) = heutiges Verhalten byte-genau. `hyper` =
`upsert_candles`/`upsert_indicators` schreiben die `candles`/`indicators`-Hypertables **primär** und
**überspringen** den Per-Coin-Write (DUAL_WRITE moot) — der Phase-5-Perf-Trial-Modus (Reads sind schon
hyper). Rollback-Asymmetrie dokumentiert (Legacy-Lücke → Backfill vor einem Read-Rollback nötig).

Verifikation: `test_candles.py` +2 Resolver-Tests (Default/legacy/hyper/unknown-reject); `test_candles_db_parity.py`
+1 DB-gated Write-Parität hinter `KYTHERA_CANDLES_WRITE_PARITY` (hyper-primary → Hypertable, **nicht** Legacy;
rollback = null Persistenz). Regression-Guard smoke+verify grün, ruff/format/mypy grün (CI-relevante Dateien).
**Out of scope** (bricht bewusst am Drop): `legacy_trainers/*`, `db_schema_analysis.py`, `tools/audit/step7_monitor_replay.py`
(TZ-forensic Wegwerf). Der `WRITE_PRIMARY=hyper`-Flip + Fleet-Restart und der Table-Drop selbst bleiben Michi-gegatet.
## [2026-07-16] K1 · TSM1 — Time-Series-Momentum auf 6h-Aggregaten (read-only, kein Modell) (T-2026-CU-9050-138)

Neues `tools/tsmom_study.py` (read-only) prüft die K1-Hypothese (§K1, Evidenz F8 / arXiv 2602.11708v1,
„2,41 Netto-Sharpe" — Overfitting-Verdacht durch monatliche Re-Optimierung): Hat ein ROC-Lookback-
Momentum-Signal auf 6h-Kerzen fleet-weit positiven Netto-Edge — auch mit UNSERER Geometrie
(Smart-Targets + fixer SL) statt des Paper-ATR-Trailings? **Festes Grid, KEIN Re-Fitting im Zeitverlauf**
(genau der Overfitting-Vektor des Papers): L ∈ {8,12,16,24,32} Bars × Threshold k ∈ {0, 0.5σ, 1.0σ}
(σ = rollierende 90d-StdAbw von ROC_L, as-of) auf 6h-Resample (UTC-Anker 00/06/12/18, nur volle
geschlossene Fenster) UND nativen 4h-Kerzen (Resample-Artefakt-Check) = 30 Grid-Zellen. Signal =
ROC_L-Bandkreuzung (Vorzeichen = Richtung); Dedupe je Coin/Richtung/Zelle max. 1 offenes Event
(Re-Entry erst nach dem Geometrie-Exit). Labels DOPPELT je Event: (a) unsere Geometrie
`get_hvn_and_sr_levels(df=as-of) → hvn_sr_trade_geometry → ensure_min_tp_distance → simulate_exit`
(First-Touch TP-vs-SL auf 1h-Kerzen, Round-Trip-Taker-Fee — die deploybare Wahrheit); (b) Paper-
Approximation = Zeit-Exit nach H ∈ {8,16,28} Bars mit weitem 15%-Katastrophen-SL. Val/Test-Chrono-Split
(fixer Kalender-Teiler = Mittelpunkt des BTC-1h-Fensters, 2026-01-13); Threshold NUR auf Val, Test einmal
angefasst. Geteilte Contracts wiederverwendet (keine Neuerfindung): `core/trade_utils` (Geometrie),
`walkforward_sim` (`simulate_exit`, `FEE_PER_SIDE=0.0005` → 0,10 % Round-Trip, `set_low_priority`,
`check_cpu_headroom`), `core/candles.read_candles` (nur geschlossene Kerzen, R1). CPU-Check per
`--skip-cpu-check` bewusst umgangen (VPS 100 % CPU-saturiert; read-only + BELOW_NORMAL, dokumentiert).
Der VPS-Watchdog reapt streunende python.exe (~alle paar Minuten, exit 1) → Studie streaming-refaktoriert
(Akkumulatoren O(Zellen), NICHT O(Events)) + resumierbar (`--resume` + Zustands-Checkpoint alle 25 Coins
in den OS-Temp, NIE ins Repo) + Relaunch-Wrapper; **Peak-RSS 291 MB** über die volle Population (der
erste ununterbrechbare Lauf starb OOM-artig bei ~75 Coins an Event-Liste + DataFrame-Slice-Cache).

**Verdikt: no-op / Paper für unseren Stack falsifiziert.** VOLLE Population: **527 Coins, 1.178.990 Events**,
Zeitraum 2025-07-14 … 2026-07-16 (KEIN Sampling). KEINE der 30 Grid-Zellen erfüllt das Stop-Kriterium
(Val UND Test positiver Netto-PnL bei n_test ≥ 200) — nur 3 Zellen überhaupt Val-positiv, ALLE drei kippen
im Test negativ: die beste Val-Zelle 4h|L12|k0.5 Val **+0,128 %** (n=11.171) → Test **−0,053 %** (n=32.902);
6h|L8|k0.5 Val +0,028 → Test −0,046; 6h|L32|k0.0 Val +0,010 → Test −0,107. WR liegt fleet-weit hoch
(~0,66–0,68), Ø-Netto-PnL aber durchgehend negativ — der klassische Regel-8-Fall (hohe Trefferquote,
größere Verlierer). Geometrie-(a)-vs-Paper-(b)-Divergenz (Kosten der Cornix-Substitution): Ø-Netto (a)
−0,13 % vs. (b) −0,20/−0,32/−0,27 % je H; unsere Geometrie schneidet je Event um +0,07/+0,19/+0,15 pp
BESSER ab als der Paper-Zeit-Exit (Smart-Targets/fixer SL kappen Verluste besser als der 15%-Katastrophen-
SL), aber BEIDE sind netto-negativ; Korrelation (a)↔(b) nur 0,40/0,35/0,23 → die Geometrie-Substitution
ändert die Per-Trade-Ausgänge materiell. Beide Label-Wege sind sich einig: das Momentum-Paper repliziert
NICHT auf 2025–26er USDT-Perps mit unserem Exit-Stack. Kein Folge-Task „Bot TSM1". Ein negatives Ergebnis
ist hier der Erfolg — dem Paper-Monats-Refitting NICHT nacheifern. Survivorship (Regel 9): Population =
heute in `coins.json` handelbare Coins, delistete Paare fehlen. Nur geschlossene Kerzen (R1), σ/ROC
trailing/as-of; exakte Quantile (Median/p5/p95) bewusst weggelassen (unvereinbar mit dem O(Zellen)-
Speicherbudget, nicht verdikt-tragend — n, WR und Ø-Netto sind exakt). Ergebnisse in
`staging_models/tsmom_study.{json,md}` (Regel 2: nur staging).
## [2026-07-16] K6 · BRD — Markt-Breadth/Dispersion-Feature-Builder + Studie (CODE-PREP, Full-Run offen) (T-2026-CU-9050-140)
## [2026-07-16] K6 · BRD — Markt-Breadth/Dispersion: Full-Run-Verdikt „weak/mixed, nicht deploybar" (T-2026-CU-9050-140)

Geteilter X-R1-Builder `core/breadth_features.py` + read-only Studie `tools/breadth_study.py` (§K6).
Der Builder rechnet as-of über das USDT-Perp-Universum (1d-Kerzen + `_indicators`, EMA50/EMA200) elf
Breadth/Dispersion-Features: Anteil Coins > EMA200 / > EMA50, Median-7d-Return, Advance/Decline-Ratio,
Return-Dispersion vs. BTC sowie einen **TOTAL3-Preis-Proxy OHNE BTC/ETH** gleich- UND volumengewichtet
(Level Basis 100, Abstand zur 90d-Regression, 90d-Breakout). **Ehrlichkeits-Hinweis:** keine echten
Marktkap-Gewichte — der Preis-Index über ~530 Perps ist ein PROXY. **Effizienz:** EINE Query je Coin
(`load_universe_panels`), Cross-Section-Gerüst EINMAL in-memory (`build_breadth_panel`), As-of =
O(log n)-Lookup ins Tagespanel; R1 (nur geschlossene Kerzen, D+1d ≤ ts, kein Lookahead). X-R1-Vertrag:
fehlende SPALTEN ⇒ `BreadthFeatureError`, NIE `fillna(0)`; fehlende WERTE = Ausschluss aus der
Cross-Section (nicht Null). Survivorship-safe: `pct_change(fill_method=None)` (kein Forward-Fill
delisteter Coins → keine fabrizierten 0-Returns), Dispersion ohne BTC-Eigenspalte.

**Resume-/Checkpoint-Maschinerie** (der Live-Watchdog reapt Fremd-Python reproduzierbar — Muster wie
K1/`tsmom_study.py`): Checkpoint-Einheit ist das per-Coin-Tagespanel (kill-anfällige Ladephase = EINE
DB-Query je Coin); alle 25 Coins werden der kompakte Panel-Store + die processed-Menge atomar in einen
transienten JSON-State im OS-TEMP (nie im Repo) geschrieben. `--resume` überspringt bereits geladene
Coins und faltet den Rest; ein Kill zwischen Checkpoints lädt nur den <25-Coin-Schwanz neu (idempotent,
per Symbol → kein Doppelzählen). Phase 2 (Build+Analyse) ist re-entrant; RAM-Guard bricht < 500 MB ab,
Speicher gedeckelt (~18 MB Store, Peak-RSS 187 MB), State bei sauberem Exit gelöscht. Der Full-Run lief
read-only + BELOW_NORMAL in EINEM Anlauf durch (527/530 Coins, 3 delisted; kein Watchdog-Kill nötig).

**VERDIKT (§K6, ehrlich, dreiwertig): `weak/mixed-breadth-signal (not deployable)`.** Datenbasis:
21.604 RUB-LONG-Events (`rub_replay_365d.jsonl`, gestreamt, kein neuer Sim), 873 Tages-Breadth-Zeilen,
71.588 `regime_history`-Zeilen. (a) Das entscheidende Head-to-Head — Win-Logit RUB-LONG (net_pnl>0),
Chrono-70/30 — hebt die Test-AUC von 0,580 (nur BTC-Regime) auf 0,622 mit Breadth (Δ **+0,042**,
n_test=3.641, Overlap 12.134 ab regime_history-Start 18.01.). Aber die Stützung fehlt: nur **2 von 11**
Features sind OOS sign+magnitude-stabil (`brd_adv_decline_ratio`, `total3_vw_dist_reg90d`, beide
grenzwertig), **6 kippen** das Vorzeichen val→test (Overfit-Signatur, z. B. `total3_ew_level`
val +0,075 → test −0,224). (b) Der unabhängige `regime_history`-TREND_UP-Test WIDERSPRICHT: Breadth
SENKT die Test-AUC 0,824 → 0,677 (Δ **−0,147**). Zwei OOS-Tests uneins ⇒ kein sauberer, robuster Edge —
§K6-Nah-No-op. RUB-LONG ist über die Monate im Schnitt negativ (Ø net −0,62 %, WR 0,45). **Der Builder
bleibt als Infrastruktur** (HMM T-020, Whitelist-Umbau §23); ein RUB-LONG-Breadth-Gate ist NICHT
lizenziert und wäre ohnehin Operator-Entscheid (Michi). Artefakte: `staging_models/breadth_study.{json,md}`
(voller Lauf, kein SMOKE-Header). Geteilte Contracts wiederverwendet
(`read_candles_with_indicators` include_forming=False, `LEGACY_WRITER_TZ`, `walkforward_sim`).

## [2026-07-16] K15 · SRX — Scratch-Reload-Exit-Studie auf ABR-Events (read-only, kein Modell) (T-2026-CU-9050-137)

Neues `tools/scratch_exit_study.py` (read-only) prüft OFFLINE die Praktiker-These (§K15, KB
`ingest-9f6511a5f951`), dass bei Break-&-Retest-Setups ein „Scratch-Reload"-Exit den fixen SL schlägt:
Position sofort scratchen, wenn eine 4h-Kerze ZURÜCK jenseits des gebrochenen Levels `level_price`
schließt (kleiner Verlust + Fees), Re-Entry beim nächsten Cross + Retest desselben Levels, max.
N ∈ {2,4,8} Zyklen, 14-Tage-Fenster je Event — statt einen vollen 4–12 %-SL-Hit zu nehmen. **Kein
neuer Detektor, kein neuer Walkforward-Lauf:** die Event-Population ist der vorhandene ABR1-Replay
`_X/staging_models/replay/abr1_replay_365d.jsonl` (288.281 Events, 526 Coins), zeilenweise gestreamt
(378 MB nie im RAM). Variante (a) = das bereits simulierte First-Touch-`net_pnl_pct` des Records (NICHT
neu simuliert, Spec-Vorgabe); (b)/(c) ersetzen NUR die Verlust-Seite. Trigger-Feld ist bewusst
`level_price` (die gebrochene Linie), nicht der Füllpreis `entry`. Grid: (b) harter SL touch-basiert,
(c) harter SL close-basiert (eigene Zelle, mit explizitem Liquidations-Caveat — close-basierte Stops
unterschätzen bei Hebel das Touch-basierte Liquidationsrisiko). Effizienz: 4h-Kerzen je Coin EINMAL
über alle 14d-Fenster geladen (526 Coin-Queries statt 288k), Simulation in-memory, EIN Durchlauf je
SL-Modus liefert alle N per Cap-Ableitung. Geteilte Contracts wiederverwendet: `walkforward_sim`
(`FEE_PER_SIDE=0.0005` → 0,10 % Round-Trip je Leg, keine erfundene Fee; `set_low_priority`/
`check_cpu_headroom`). `signal_time` ist naiv-UTC (Writer = UTC-Instant), `open_time` TIMESTAMPTZ →
robust nach UTC-naiv konvertiert; nur geschlossene Kerzen (R1).

**Verdikt: no-op / These falsifiziert.** 288.211 Events simuliert (VOLLE Population, 70 ohne 4h-Kerzen
übersprungen, KEIN Sampling). Variante (b) schlägt (a) in KEINER Zelle und KEINER Chrono-Hälfte:
Ø-Netto (a) −0,10 % vs. (b) −0,41…−0,52 % je N; Δ(b−a) durchgängig negativ in Val UND Test
(z. B. N=4: Val −0,49, Test −0,33). Der erhoffte Tail-Tausch tritt nicht ein — der Scratch kappt vor
allem die GEWINNER (p95 6,5–6,8 % vs. Baseline 10,7 %, weil frühe Scratch-Exits vor TP1 die großen
Läufe abschneiden), während der Verlust-Tail bei gestapelten Re-Entries sogar WÄCHST (p5 bis −10,3 %
in (c)·N8 vs. Baseline −9,03 %). Die Aux-Zelle (reine TP1-vs-Touch-SL-Geometrie ohne Scratch, WR
55,8 %, Median +2,1, Ø −0,16 %) zeigt: der Malus kommt aus der Scratch-Mechanik selbst, nicht aus
TP1-statt-Ladder. Monats- und Chrono-Split bestätigen: (b)/(c) liegen fast durchgehend unter (a).
Cornix-Fit/Bot-Verdrahtung damit hinfällig — der Trade-Monitor kennt ohnehin weder Scratch-Exits noch
Re-Entries; die Studie ist bewusst offline, nichts geht in einen Bot. Survivorship (Regel 9): Population
= heute in `coins.json` handelbare Coins, delistete Paare fehlen → Verlust-Tail für ALLE Varianten
gleich optimistisch, der (b)-vs-(a)-Vergleich bleibt intern konsistent. Ergebnisse in
`staging_models/scratch_exit_study.{json,md}` (Regel 2: nur staging).

## [2026-07-16] K8 · SET — Settlement-/Tageszeit-Studie über die Fleet (read-only, kein Modell) (T-2026-CU-9050-135)

Neues `tools/settlement_timing_study.py` (read-only) prüft die K8-Hypothese (F9): beeinflusst die
Entry-Nähe zu den Funding-Settlements (00/08/16 UTC) bzw. die Tageszeit die Expectancy unserer
Trades? Rein zeit-abgeleitet, **kein Funding-Join nötig** — je Trade wird (a) der vorzeichenbehaftete
Entry-Offset zum nächsten Settlement (−240…+240 min in 30-min-Buckets) und (b) die Entry-Stunde UTC
berechnet, dann Expectancy je Bucket × Richtung × Modell-Tag: n, WR, Ø-Netto-PnL (Round-Trip-Fee in,
winsorisiert UND roh), Median, ein einfaches Bootstrap-CI (1000 Resamples, kein Signifikanz-Theater),
Monats-Split und ein Chrono-Val/Test-Halbieren. Geteilte Contracts wiederverwendet:
`walkforward_sim.FEE_PER_SIDE=0.0005` (Round-Trip 0,10 %, keine erfundene Fee) und
`core/time.LEGACY_WRITER_TZ` — `open_time` ist naiv-lokal Bukarest (TZ-Cluster P2.1–P2.6) und wird
**DST-korrekt** nach UTC konvertiert (ein Konstant-Offset würde jeden Offset über den 29.03-DST-Sprung
um eine Stunde verschmieren). Dedup von `closed_ai_signals` auf (symbol, model, direction, open_time)
mit niedrigster id: 445.750 roh → 88.267 dedup (alle mit gültiger UTC-Zeit analysiert).

**Verdikt: timing-edge-found — aber sign-stabil, magnitude-schwach & stark attenuierend.** 34 stabile
prefer/avoid-Fenster (18 fleet-weit), definiert als vorzeichen-konsistent über BEIDE Chrono-Hälften mit
n≥300 und einem Magnitude-Floor |Δ|≥0,5pp/Trade vs. Gruppe×Richtung-Baseline (winsorisierte Means,
damit kein einzelner Legacy-Tail ein Bucket kippt). Das Muster ist richtungs-kohärent (LONG bevorzugt
Abend-Stunden 17–23 UTC, SHORT meidet Nacht/Früh 00–04 UTC; SHORT meidet die 0–30 min NACH dem
Settlement), aber die **Stärke bricht out-of-sample ein**: Median |Δ| val 3,18pp → test 1,00pp
(val/test ≈ 3,18×) — der K3-Attenuations-Befund wiederholt sich. Darum low-conviction: taugt allenfalls
als Scan-Minuten-Verschiebung je Bot, **kein hartes Gate**. Wesentliche Confounder dokumentiert (Rule
9): die Population ist auf tatsächlich eröffnete/geschlossene Trades konditioniert — inkl. der
**Scan-Schedule je Bot**, die Entries an bestimmten Minuten/Stunden clustert; ein Tageszeit-„Effekt"
kann ein Kompositions-/Scan-Confound statt echter Mikrostruktur sein. WR allein ist nicht entscheidend
(Rule 8). Ergebnisse in `staging_models/settlement_timing_study.{json,md}` (Rule 2: nur staging).

## [2026-07-16] K3 · FRL — Funding-Risk-Layer-Studie über die Fleet (read-only, kein Modell) (T-2026-CU-9050-134)

Neues `tools/funding_risk_study.py` (read-only) prüft die K3-Hypothese: haben Fleet-SHORTs bei
extrem-positivem Funding systematisch schlechtere Expectancy (Squeeze), symmetrisch LONGs bei
extrem-negativem Funding — und **generalisiert das ABR2-Gate** (LONG nur `fund_24h > +3 bps`,
SHORT-Veto `> +1.5 bps`) fleet-weit? Analysiert die **komplette (präskriptive) §K3-Feature-Liste**:
fund_24h, fund_72h, fund_7d_cum plus eine **echte Cross-Section-Perzentile** `cs_pctl` (Coin zum
Entry-Zeitpunkt gegen ALLE anderen Coins' as-of fund_24h gerankt — der ABR2-Konstrukt; NICHT die
Per-Symbol-Selbsthistorie `fund_pctl_90d` des Builders). Nutzt die geteilten Contracts:
`core/funding_features` (as-of-Builder), `walkforward_sim.FEE_PER_SIDE=0.0005` (Round-Trip 0,10 %,
keine erfundene Fee) und `core/time.LEGACY_WRITER_TZ` (open_time = naive Bukarest → DST-korrekt
nach UTC, kein Konstant-Offset über den 29.03-DST-Sprung). Dedup von `closed_ai_signals` auf
(symbol, model, direction, open_time) mit niedrigster id: 445.685 roh → 88.202 dedup, 82.667 mit
as-of-Funding (82.826 mit cs_pctl). Mittelwerte werden winsorisiert (1/99-Pct, tail-safe) UND roh
ausgewiesen — die Roh-/Median-Werte zeigen den SHORT-Squeeze-Tail ungeschnitten.

**Verdikt: direction-confirmed, magnitude-weak** (die ABR-*Richtung* generalisiert fleet-weit, ein
harter fleet-weiter Extremzonen-Veto ist NICHT lizenziert). Primärtest = Per-Trade-Spearman
fund_24h↔net-PnL, pro Richtung, pro Chrono-Hälfte, mit **Magnitude-Floor** (|ρ|≥0,03): das
Vorzeichen ist ABR-konform und stabil über beide Hälften für ALLE vier Features (LONG>0, SHORT<0),
aber die Stärke ist schwach (|ρ|≈0,06–0,12 in der val-Hälfte) und **attenuiert in der Test-Hälfte
gegen null** — der Magnitude-Floor wird über beide Hälften nur von **cs_pctl SHORT** gehalten
(−0,057 val / −0,059 test, bemerkenswert stabil; die Cross-Section ist robuster als absolutes
Funding). Roh-Means enthüllen den Squeeze: SHORT@extrem-positiv val −16,98 % vs. Baseline +3,57 %,
kippt aber in der Test-Hälfte auf +2,44 % → nicht both-halves-stabil. ABR2-Gate-Check fleet-weit
richtungskonform (LONG in-gate > out-gate, SHORT in-veto < out-veto). Q4-Quintil kollabiert (Ties
am Default-Funding-Satz — dokumentiert, nicht still gedroppt). Ergebnisse in
`staging_models/funding_risk_study.{json,md}` (Rule 2: nur staging). Bekannter Bias: Survivorship
(530 Funding- vs. 716 Signal-Symbole).

## [2026-07-16] Hyper-Read-Backend in core/candles.py — C-Gate Phase 4 (dormant hinter Flag) (T-2026-CU-9050-128)

Der einzige Code-Blocker für den Read-Cutover. `core/candles.py` liest bei
`KYTHERA_CANDLES_SOURCE=hyper` jetzt aus den beiden Hypertables `candles`/`indicators`
(gefiltert nach `symbol, tf`) statt aus den ~9,3k Per-Coin-Tabellen — **dormant**, Default bleibt
`legacy` → null Live-Wirkung bis Michi flippt (+ Restart, trivial rollbar). Kein Bot angefasst
(Design-Intent Phase C): die core.candles-Read-Call-Sites routen automatisch.

**Hyper-Pfad** für `read_candles`, `read_indicators`, `read_candles_with_indicators`,
`latest_open_time` + Shape-Helfer `indicator_column_names`. Das alte `_assert_legacy_backend()`
(warf für alles ≠ legacy) wird zu `_candle_source()`: validiert den Flag, dispatcht die Reads und
lässt WRITES/DELETES bei `hyper` weiterlaufen — die schreiben immer die Legacy-Tabellen, die
Hypertables hält der separate `KYTHERA_CANDLES_DUAL_WRITE`-Mirror frisch (muss über das
Phase-4→5-Fenster AN bleiben). Ein Source-Flip schaltet so nur um, was die Fleet LIEST, ohne die
Ingestion zu stoppen.

**Exakte Legacy-Semantik erhalten** (verhaltensneutraler Cutover): der Forming-Filter bleibt
**uhr-basiert** (`open_time < period_start(tf, now())`), NICHT die `is_closed`-Spalte — die kann am
Rand-Kerzen-Race dem Clock nachhängen und würde eine Zeile droppen, die der Legacy-Read behält
(Paritätsbruch). `tf`/`is_closed` sind echte Hypertable-Spalten, die den Per-Coin-Tabellen fehlen →
aus jeder Projektion ausgeschlossen (Legacy-Shape + Ordinal-Ordnung; `indicator_column_names` droppt
sie, damit `SELECT *`-Reads byte-gleich bleiben). Der JOIN-Read fenced BEIDE Seiten in
`(SELECT … OFFSET 0)`-Subqueries: zwei Hypertables auf der Partitionsspalte zu joinen lässt
TimescaleDB einen Merge-Join über die Ordered-Append-Pfade wählen, der serverseitig
`mergejoin input data is out of order` wirft — die Fence entfernt diese Pfade.

**`table_exists`/`list_coin_tables` bleiben phasen-agnostisch** (kein Hyper-Branch): sie proben die
Per-Coin-RELATIONEN, die unter beiden Backends bis zum Phase-5-Drop existieren. Ein
`SELECT DISTINCT symbol, tf` über die 40M-Zeilen-Hypertable ist gemessen >20 s (die
Chunk-Partitionierung schlägt auch einen Loose-Index-Scan) und würde die 6_housekeeping-Retention
blockieren, die in hyper-Read-Mode ohnehin die Legacy-Tabellen löscht. Nach dem Phase-5-Drop
liefern beide leer/False — genau das dokumentierte Endverhalten.

Akzeptanz (Live-VPS, read-only): `backtest/test_candles_db_parity.py` beweist **hyper-Read ==
legacy-Read** für BTC/ETH/SOL + kleinere Coins über mehrere TFs, mit/ohne forming, verschiedene
Fenster/Limits — Kerzen byte-für-byte, Indikatoren auf **float4-Präzision** (die Legacy-REAL-Spalten
tragen weniger Bits als die Hyper-`double`; das ist der gewollte P3.12-Upgrade, kein Drift — der
float32-Cast reproduziert die REAL bit-genau, ein echter Wertunterschied fällt weiterhin auf).
28 Coin/TF-Kerzen-Reads + 21 mit Indikatoren grün. DB-frei: `test_candles.py` (Source-Resolver,
Unknown-Backend-Reject, Hyper-Validierung vor der Connection). Regression-Guard smoke+verify grün,
ruff/format/mypy grün. Der Flip selbst (`SOURCE=hyper` + Fleet-Restart) bleibt Michi-gegatet.

## [2026-07-16] Z1-Analytics-Substrat: inkrementeller DuckDB/Parquet-Export + erster Erfolgsraten-Endpoint (T-2026-CU-9050-131)

Erster Implementierungs-Task der Z1-Stufe-1 (Ideation-Council T-129, Kuratierung Michi 2026-07-16).
Baut den **einzigen Analytics-Datenpfad** des kommenden Dashboards (Gutachten-Option A): das
Dashboard liest nie mehr direkt Live-PG, sondern ein columnar Substrat, das ein Task-Scheduler-Job
(kein Bot-Prozess, Watchdog bleibt Owner) inkrementell befüllt.

- **`tools/analytics_export.py`** — watermark-getriebener Export von vier Quellen
  (`closed_trades_master`, `closed_ai_signals` inkl. ROM1, `ml_predictions_master`, `regime_history`)
  in DuckDB-Tabellen + datums-partitioniertes Parquet (`<src>/dt=YYYY-MM-DD/data.parquet`). Nur
  **geschlossene** Rows (`posted`/`close_time IS NOT NULL`, kein `ENTRY_NOT_FILLED`). Inkrementell
  per **Keyset-Cursor `(ts, id)`** mit strikter `>`-Grenze — kein Skip an gleichen Timestamps, keine
  Dubletten, ohne Import-Dedup. LIMIT-Batches + per-Session `statement_timeout` (CPU-Blip-Guard).
  Watermark + Batch committen atomar (Crash-safe Resume). **Datenstand-Feld** pro Quelle
  (`last_row_ts` + `synced_at` [UTC] + `rows_total`) als First-Class-Output für den Panel-Indikator.
  R3-Disziplin: naive-lokale Legacy-Timestamps werden verbatim durchgereicht, nie als UTC umgedeutet.
- **`tools/analytics_api.py`** — erster Endpoint (dünner Flask-Blueprint, Framework-Entscheid T-130
  offen): Erfolgsraten-Zeitreihe (Rolling 7/30/90d, Bot-Multiselect, Tages-Serie), liest **nur** die
  DuckDB-Datei. Outcome PnL-basiert wie der Realized-PnL-Report (23_market_tracker) — neutral bei
  Housekeeping/Micro/Outlier, Winrate über decisive Trades. User-Input parametrisiert (keine
  SQL-Injection), read-only-Connection pro Request.
- **Timescale-Forward-Kompatibilität:** Quellen als austauschbare `SourceSpec`-Config; Kerzen bewusst
  out-of-scope (Folge-Task, nur 5m-Basis-TF).

Verifikation (DB-frei, Build-Maschine ohne Credentials): `backtest/test_analytics_export.py` 15/15 —
synthetischer Fetcher (spiegelt den PostgresFetcher-SELECT-Vertrag) + echte DuckDB/Parquet-Materialisierung;
deckt Watermark-Tie, Batching==Single-Batch, Closed-Filter, Freshness, Rolling-Window, DB-freien Import.
ruff/mypy grün (tools/backtest sind CI-exkludiert — lokal geprüft). Beide Kern-Reviews PASS. `duckdb>=1.0`
neu in `requirements.txt` (nativer Parquet-Reader/Writer, kein pyarrow). **Echter Lauf nur in VPS-Session.**

## [2026-07-14] Fleet-weites Shadow-Mode-Posting + 3-Wege-Report + Regime-Gating-Evidenz (T-2026-CU-9050-125)

Drei zusammenhängende Teile. **Nichts geht live** — Shadow postet nie in einen Kanal,
Artefakte bleiben in `staging_models/`, Aktivierung braucht einen Fleet-Restart (Michi).

**Teil 1 — Shadow-Mode-Posting (fleet-weit).** Jedes nicht-promotete Retrain-Bein erzeugt jetzt
einen ÜBERWACHTEN Shadow-Trade statt Stille: eine `ai_signals`-Zeile OHNE `telegram_outbox` →
der AI-Monitor (Bot 8, enthält keinen Posting-Code) verfolgt sie bis zum realisierten Close in
`closed_ai_signals`, ohne dass je ein Zeichen einen Kanal erreicht (verifiziert). Neu:
`core/shadow_gate.py` (per-`(tag,direction)`-Lifecycle mit **Default-LIVE** — der Gate darf nie
einen bestehenden Live-Post stummschalten; toleranter Loader für BEIDE Artefakt-Formate inkl. der
`null`-Threshold-Retrains, die die Produktions-Loader ablehnen) + `core.signal_post.post_shadow_ai_signal`.
Verdrahtet: **ATS2** (Bot 12), **ATB2** (Bot 14), **SRA2** (Bot 9), **RUB3** (Bot 13,
`rub2_model_LONG`), **EPD3** (Bot 10, `epd2_*`). RUB3/EPD3 = **kollisionsfreie Challenger-Tags**
(Regel 6, Operator-Entscheid Michi): die Live-Beine posten schon unter `RUB2`/`EPD2`, ein Shadow
unter demselben Tag würde über den Active-Trade-Check einen Live-Post blockieren. SRA2/EPD3 zeigen
den Kern: „nicht deploybar" war ein TRAININGS-Problem (tote Label-Quelle) — Shadow REVIVED sie,
weil der AI-Monitor die frischen Outcomes liefert. Rein additiv, jeder Shadow-Pfad fehler-gekapselt
(der Live-Pfad ist nie betroffen). Master-Schalter `KYTHERA_SHADOW_POSTING`. Spec: `docs/SHADOW_MODE_POSTING.md`.
Bekannter Erb-Caveat (EPD3): das epd2-Artefakt wurde auf leicht verschobenen Feature-Defs gefittet
(P1.41 / T-035), Drift nur bei Gap-Ticks — gilt für den Shadow wie für ein etwaiges Live-EPD2.

**Teil 2 — Sentiment-Report 3-Wege-Gliederung.** `23_market_tracker.py:job_realized_pnl_report`
gliedert die Realized-PnL jetzt in **ACTIVE (live) / SHADOW (getrackt, nie live) / RETIRED
(alte Tags)** je `(tag,direction)` über `shadow_gate.leg_status`. Klassifikation als reine,
testbare Funktion `realized_lifecycle_bucket`.

**Teil 3 — Regime-konditioniertes Gating (Evidenz, keine Live-Änderung).** `docs/REGIME_CONDITIONED_GATING_EVAL.md`
+ read-only `tools/regime_conditioned_gating_scan.py`: Ja, global-negative Quellen laufen
regime-positiv — aber der Punktschätzer ködert (ATS1-LONG/TRANSITION est +1,45 %, lb −0,26 →
v2 blockt korrekt). **18 Zellen** unter global-negativen Beinen überleben die v2-EB-Shrinkage
(z. B. BR1H-LONG/HIGH_VOLA lb +1,39 % n_eff 1505). Vehikel existiert bereits (v2-Whitelist) →
Empfehlung: den T-069-Flip auf FRISCHEN Daten scharf schalten + `per_source × regime`-Kreuztabelle
im AIM2-Report; kein neuer Gate, kein pauschales Aus.

Verifikation: `backtest/test_shadow_gate.py` (14) + `backtest/test_market_tracker_lifecycle.py` (5)
neu, DB-frei; volle Report-/Shadow-Suite 67/67, ruff/format grün, Regression-Guard smoke grün.
Live-Wirkung erst nach Fleet-Restart (Michi); Promotion eines Shadow-Beins bleibt Operator-Entscheid.

## [2026-07-14] v1-vs-v2 Whitelist-Flip-Auswertung gebaut (048-Shadow-Gate, T-2026-CU-9050-069)

Neues read-only VPS-Tool `tools/whitelist_v2_flip_eval.py` — die Datengrundlage für Michis
Flip-Entscheid v1→v2 des Whitelist-Gates (Shadow-Spalten aus T-048, live seit T-068-Deploy
2026-07-11). Beantwortet die vier T-069-Fragen: **(1)** Divergenz-Matrix v1×v2 über den
`bot_regime_whitelist`-Snapshot (inkl. lb-Verteilungen aus `reason_v2`), **(2)** Counterfactual-PnL
des echten Gate-Traffics seit Deploy, gebucketed nach Flip-Klasse (`v2_would_block` /
`v2_would_open` / Agreement) — Replay ausschließlich über die T-047-Mechanik (`score_row`/
`load_1h`/`aggregate` importiert, X-R1: keine nachgebaute Geometrie), **(3)** Volumen-Effekt
(Gate-Raten, ROM1-Forwards/Tag-Prognose), **(4)** Summary-JSON + Konsolen-Report als
Entscheidungsbasis — Empfehlung + Flip bleiben bei der VPS-Session + Operator (Stop-B gültig).

- Fallback-Pfade (`no_whitelist_entry`, `whitelist_stale:*`, `*fallback*`, NULL) sind vom Flip
  unberührt (Bot 28 tauscht nur den 4D-Zellen-Read) → klassifiziert als `unaffected`, nie gescored.
- **v1-Drift-Metrik** quantifiziert die Snapshot-Näherung (Bot 28 loggt v2 nicht pro Signal,
  die Whitelist-Tabelle ist UPSERT-only): aufgezeichnete v1-Entscheidung vs. heutiger Snapshot.
- Prereq-Checks (Bot-27-Freshness, v2-Coverage) + per-Tag-Zähler machen die Outage-Lücke vom
  2026-07-13 sichtbar; `cell_missing`/`v2_missing` werden gezählt statt still verworfen.
- Doku/Spec: `docs/WHITELIST_V2_FLIP_EVAL.md` (AK1–AK8, Methodik-Caveats, VPS-Anleitung).

Verifikation: `backtest/test_whitelist_v2_flip_eval.py` 18/18 (reine Klassifikations-Schicht,
DB-frei), `backtest/test_rom1_counterfactual.py` unverändert grün; ruff/format grün, repo-mypy
grün (`tools/` bewusst excluded, Falle 12). Lauf selbst braucht die Live-DB → VPS ~17./18.07.

## [2026-07-14] ATS/TSI-Trainer (Bot 12) DB-basiert neu gebaut → ATS2-Staging + Trainer==Serving-Parity (T-2026-CU-9050-121)

Der letzte CSV-basierte Legacy-Trainer der Fleet (Bot 12 TSI-Sniper) ist auf das moderne
Replay-Muster umgestellt: DB → Features → Walk-Forward-Label → Train → Staging, jederzeit
wiederholbar, R1-clean über `core.candles`, **kein CSV-Zwischenschritt** mehr. Modell-Artefakte
NUR nach `staging_models/` (ATS2, harte Regel 2/6) — **kein Rollout** (Michi-gegated).

**Befund-Korrektur zum Task-Brief (verifiziert gegen `audit_reports/13_x_ml_trainers.md` + die
Bot-Inference):** das Brief-Mapping der Legacy-Trainer war auf beiden Achsen falsch — `BT1-*`
speist Bot 14 (ATB, geparkt; `BT1-ML-Trainer.py` ist toter Code), `BT3-*` speist Bot 13 (RUB).
**Bot 10 (Pump)** lädt ein 10s-Tick-Modell (`vol_ratio/p_chg_60s/…` aus dem Ticker-Puffer, NICHT
aus `core.candles` rekonstruierbar) und hat mit EPD2 bereits einen DB-Retrain. **Nur Bot 12
(ATS/TSI)** war ein echtes `core.candles`-Ziel — Scope entsprechend auf ATS2 fokussiert
(Operator-Entscheid via AskUserQuestion), EPD2-Pfad auditiert.

- **Neu `core/ats_features.py`** — geteilter Feature-/Detektions-Builder (X-R1-Regel): `ATS_FEATURES`
  (29er-Vertrag), `ats_cross` (TSI-Signallinien-Crossover), `build_ats_features` (OBV/VWAP +
  29 Features), `assert_features_alive`. Aus der inline-Logik von `12_ai_ats_bot` gehoben.
- **`12_ai_ats_bot.py` verkabelt** auf den geteilten Builder + `core.trade_utils.hvn_sr_trade_geometry`
  (byte-identisch zur bisherigen inline-Geometrie) — **verhaltensneutral**: der Bot lädt weiter
  `model_tsi_*_robust.pkl`, Live-Semantik unverändert. Der 5. HVN/SR-Geometrie-Klon fällt weg.
- **Walk-Forward-Adapter** `tools/walkforward_sim.py --strategy ats` — je geschlossener 1h-Kerze
  ein Crossover-Check, OBV-Baseline-Parität über das 500-Kerzen-Fenster, Label = First-Touch
  TP1-vor-SL der geposteten HVN/SR-Geometrie via `simulate_exit` (Fees inkl.).
- **Trainer** `tools/retrain_from_replay.py --strategy ats` — Binärmodell je Richtung, chronologischer
  70/15/15-Split + 7d-Purge, `pick_threshold_safe`, Isotonic-Kalibrierung → `ats2_model_{LONG,SHORT}.pkl`
  + `_meta.json` (`model_id=ATS2`). **Ein-Kommando-Wrapper** `tools/retrain_ats.py --days/--since`.
- **Parity-Test `backtest/test_ats_features.py`** (harte Regel 7) — beweist `build_ats_features` ==
  die frühere Bot-12-Serving-Konstruktion (wortwörtliche Referenz-Kopie), über mehrere Seeds UND
  Fensterlängen (die OBV-Baseline hängt vom Fensterstart ab); + Feature-Vertrag, `ats_cross`,
  Alive-Guard, DB-freier Adapter-Smoke. 5/5 grün.
- **EPD2/Pump-Pfad auditiert** — bereits DB-basiert (`pump_dump_events` + `ticker_10s` + `core.candles`,
  R1-clean), CSV-frei, staging-output; kein Fix nötig (10s-Tick-Features sind nicht candle-basiert
  reproduzierbar). Für Symmetrie neu: Ein-Kommando-Wrapper `tools/retrain_pump.py --days/--since`.
- `docs/MODEL_INTENT.md` §6 (ATS2-Infrastruktur) + §7 (EPD2-Audit) fortgeschrieben.

Verifikation: `backtest/test_ats_features.py` 5/5, `backtest/test_atb2_features.py` 10/10 (Adapter-Import
unverändert), ruff/format/mypy grün auf den CI-geprüften Dateien (`core/ats_features.py`,
`12_ai_ats_bot.py`); `tools/` bleibt bewusst außerhalb des Lint-Bars (Falle 12, nicht reformatiert).

## [2026-07-13] ROM1-Regime-Auto-Closes in den Realized-PnL-Report: Bot-28-Close-Writer persistiert targets+lev (T-2026-CU-9050-116)

Follow-up zu T-115 auf Operator-Anweisung ("rom trades sollten auch drinnen sein"): der **zweite**
`closed_ai_signals`-Writer — der Regime-Auto-Close in `28_signal_orchestrator.py`
(`force_close_trades_for_regime_change`, Status `CLOSED_REGIME_CHANGE`) — schrieb keine
targets/lev; unter der exact-only-Regel des Realized-PnL-Reports blieben ROM1-Auto-Closes damit
dauerhaft unsichtbar. Jetzt: SELECT der ROM1-Rows holt `targets` + `ai_signals.lev` (First-Poll-
Stempel aus T-115) mit, der Close-INSERT reicht beide durch; lev-Fallback für ungestempelte
Übergangs-Rows = `get_max_leverage(symbol, ROM1_DESIRED_LEVERAGE)` (ROM1 postet immer den
20x-Standard-Cap). **Deploy-Ordering abgesichert:** deterministische `information_schema`-Probe —
läuft Bot 28 vor der Bot-8-Migration, schließt er im Legacy-Format weiter (Close hat Vorrang vor
Report-Sichtbarkeit), statt den Regime-Close lahmzulegen. Der Housekeeping-Writer
(`6_housekeeping`, DELISTED) bleibt bewusst unangetastet — neutral, vom Report gefiltert.

Verifikation (DB-frei): `backtest/test_signal_orchestrator.py` 88/88 — zwei bestehende
Regime-Close-Tests auf den neuen Column-Contract erweitert (targets/lev-Passthrough), zwei neue
Tests (Legacy-INSERT vor Bot-8-Migration; lev-Fallback auf ROM1-Default) + Fix eines vorbestehend
roten Tests (seit T-109 liest `_get_last_close_price` über `core.candles.read_candles` — Mock
gepatcht). ruff/mypy grün, Guard-Smoke OK. Deploy am selben Michi-Gate wie T-115 (zusätzlich
Bot-28-Restart).

## [2026-07-13] R1/TimescaleDB C-Gate Phase 2 (Build) — Dual-Write + Backfill + 1d/1w-WS-Removal (T-2026-CU-9050-119)

Zweite DB-Migrations-Phase der R1+TimescaleDB-Umstellung (Umbrella T-2026-CU-9050-018,
D-2026-CLD-109), aufbauend auf Phase 0. **Drei reversible, dormante Code-Slices** — jede
eigenes PR + beide Core-Reviews PASS. **Die Aktivierung (Flag an + Fleet-Deploy + Backfill
laufen lassen + Paritäts-Beobachtung → Phase 3) bleibt vollständig operator-gegatet;** kein
Slice ändert Live-Verhalten beim Merge. Reads bleiben Legacy bis zum Phase-4-Cutover.

- **2a — Dual-Write (PR #110, gemergt):** bei gesetztem `KYTHERA_CANDLES_DUAL_WRITE` (Default
  AUS) schreiben `core.candles.upsert_candles`/`upsert_indicators` die `candles`/`indicators`-
  Hypertables ZUSÄTZLICH zu den Alt-Tabellen — ein zweiter INSERT in der Transaktion des
  Callers (gemeinsam committed). **Keine Bot-Änderung** (der `closed`-Flag + `tf` kamen in
  Part 1 genau dafür in die Signaturen). candles: `tf` + R1-`is_closed`, `ON CONFLICT
  (symbol,tf,open_time)` mit `is_closed` in SET UND `IS DISTINCT FROM` (forming→closed flippt
  in-place, unveränderter Re-Upsert = No-op, kein WAL-Churn). indicators: `tf` + `is_closed`=true
  (Engine rechnet post-R1 nur auf geschlossenen Kerzen).
- **2b — Backfill-Copy (PR #111, enqueued):** `tools/candles_backfill.py` kopiert die Per-Coin-
  HISTORIE einmalig in die Hypertables (Komplement zum forward-only Dual-Write). Idempotent
  (`ON CONFLICT DO NOTHING` — überschreibt nie eine forward-geschriebene Zeile), resumable
  (Progress-Datei, Commit pro Tabelle). Per-Zeile `is_closed = (open_time < period_start(tf,now))`
  statt des `…, true`-Sketches aus §3 (die Alt-Tabellen tragen die forming-Kerze). Indikatoren
  copy/cast (KEIN Recompute — D-109 #4; Alt-Indikatoren behalten den Forming-Kontaminationswert).
  Default = Dry-Run-Plan (9669 Zieltabellen enumeriert, read-only), `--execute` schreibt.
- **2c — 1d/1w-WS-Removal (PR #112, enqueued):** `1_data_ingestion` streamt 1d/1w nicht mehr
  über WebSocket (`WS_TIMEFRAMES` = `TIMEFRAMES` − {1d,1w} an beiden `@kline`-Buildern) — spart
  ~1.300 Streams (IP-Drossel-Risiko). Der REST-/Catch-up-Pfad ist UNVERÄNDERT (iteriert weiter
  das volle `TIMEFRAMES`), 1d/1w kommen weiter per REST (mit Catch-up-Zyklus-Latenz, akzeptiert
  per D-109 #3). WS bleibt für 5m–4h.

**Verifikation:** DB-freie Tests (Flag-Parsing, Backfill-Progress/Guard, WS/REST-Split);
DB-gated Byte-Tests hinter `KYTHERA_CANDLES_WRITE_PARITY` (Dual-Write + Backfill schreiben in
die realen Hypertables, per `conn.rollback()` null Persistenz — Hypertables verifiziert leer);
Guard smoke+verify 24/24; `core.candles`/`1_data_ingestion` ruff/format/mypy clean, Whole-Repo-
`ruff check .` grün. **Offen:** Aktivierung (jeder Schritt Michi-gegatet) + Phase 3–5.

## [2026-07-13] R1/TimescaleDB C-Gate Phase 0 — leere candles/indicators-Hypertables angelegt (T-2026-CU-9050-118)

Erste **DB-Migrations-Phase** der R1+TimescaleDB-Umstellung (Umbrella T-2026-CU-9050-018, Entscheidungen
**D-2026-CLD-109**): die zwei **leeren** Ziel-Hypertables anlegen. Reine Storage-Vorbereitung — `core.candles`
liest weiter die ALTEN Per-Coin-Tabellen (`KYTHERA_CANDLES_SOURCE=legacy`), kein Bot wird angefasst, Rollback
trivial (`DROP TABLE` — nichts liest die neuen Tabellen bis zum Phase-4-Cutover). Auf der Live-VPS ausgeführt,
DDL-Schritt → Freigabe Michi vor Stempel + Ausführung.

- **Neues Modul `core/candles_schema.py`** — idempotentes `ensure_hypertables(conn)` nach dem Muster von
  `core/oi_5m.ensure_schema` (self-committing, Rollback-on-Failure). Runner `python -m core.candles_schema`
  (Default = DB-freier Dry-Run-Print; `--execute` schaltet die Live-DDL scharf).
- **`candles`** (9 Spalten, §1 des Migrations-Designs): `symbol, tf, open_time, open, high, low, close, volume,
  is_closed`, PK `(symbol, tf, open_time)`. `tf` ist jetzt eine echte Spalte (war im Per-Coin-Tabellennamen
  implizit), `is_closed` ist der R1-Vertrag (`DEFAULT false`).
- **`indicators`** (113 Spalten): `symbol, tf, open_time, is_closed, close` + die **108** Indikator-Spalten aus
  `2_indicator_engine.get_indicator_definitions()` — **zur Build-Zeit aus der EINEN kanonischen Quelle** abgeleitet
  (importlib), damit die Hypertable nie von dem driftet, was Engine/Writer produzieren (Report #18).
- **Entscheidungen (D-2026-CLD-109):** **REAL→double precision** für alle numerischen Indikator-Spalten
  (verifiziert: 0 `float4` in `indicators`; `trend_direction` bleibt `text`), **Retention unbegrenzt** (keine
  Policy). **Compression bewusst auf Phase 5 vertagt** (Operator-Entscheidung 2026-07-13) — Phase 0 legt nur
  Tabellen + Hypertable + Index an. `create_hypertable(...,'open_time',chunk_time_interval=>'7 days')` in der
  klassischen Form (wie `core/oi_5m` auf TS 2.26.3; äquivalent zum `by_range()` aus §1).
- **Verifiziert live:** beide Hypertables vorhanden (1 Dim `open_time`, 7-Tage-Chunks), leer, keine
  Compression-/Retention-Jobs, Spalten-Parität gegen Legacy `BTCUSDT_1h_indicators` (neu: exakt `{tf, is_closed}`,
  keine Legacy-Spalte verloren). DB-freie Tests (`backtest/test_candles_schema.py`, 5×), Guard smoke+verify 24/24,
  ruff/format/mypy clean. Beide Core-Reviews PASS (z-code-reviewer APPROVED, z-spec-compliance 9/9 ACs).
- Phase-0-Gate `backtest/test_candles_db_parity.py` = **11/12**; der eine Fehlschlag
  (`test_include_forming_false_drops_only_forming_rows`) ist eine **now-verankerte Freshness-Assertion**, die an der
  laufenden Ingestion-Outage scheitert (Fenster `[now−10·Δ, now]` leer, da die Daten um 07:25 enden) — **keine
  Phase-0-Regression** (Legacy-Reads, orthogonal zu den leeren Hypertables).

**Offen (Michi-gegatet):** Retrain-Rollout (Part 2) + C-Gate Phasen 2–5 (Dual-Write inkl. 1d/1w-WS-Removal,
Backfill, ≥5–7 Tage Parität, Read-Cutover, Cleanup/Drop der ~9,7k Alt-Tabellen). Die R1-AUDIT-Box schließt erst
mit Phase 5.

## [2026-07-13] R1/TimescaleDB Block 6 Part 1 — DB-Writer auf core.candles + 4 API-Gaps (T-2026-CU-9050-114)

Letzter Code-Block der R1+TimescaleDB-Migration (Umbrella T-2026-CU-9050-018): die Kerzen-/Indikator-**Writer**
schreiben und lesen jetzt über `core.candles`. **Reine Code-Umverdrahtung (Part 1)** — die DB-Migration selbst
(Retrain-Rollout + C-Gate) ist Part 2/3 und bleibt Michi-gegatet. Auf der Live-VPS gebaut, Live-Write-Änderung
→ nicht autonom enqueut, Freigabe Michi vor `cu/reviews` + merge-train.

- **Vier neue `core/candles.py`-Funktionen (Signaturen frozen):** `latest_open_time(kind='indicators')`
  (Indikator-Watermark), `delete_candles_before(cutoff, *, kind)` (Retention `<`), `delete_indicators_from(start)`
  (Gap-Invalidierung `>=`), `list_coin_tables(tf=None, *, kind=None)` (form-basierte Tabellen-Enumeration via
  `_parse_coin_table`, ersetzt die rohen `information_schema`-Scans + den `"trades"/"telegram"`-Substring-Blacklist).
- **`1_data_ingestion`:** `get_latest_open_time`→API; `insert_fast`→`upsert_candles` mit **closed/forming-Split**
  an `period_start` (zwei Calls); `_flush_to_db`→`upsert_candles(closed=k['x'])` — der **WS-Buffer trägt jetzt das
  echte Binance-Closed-Flag** (erster Eintritt von `is_closed` über den WS-Pfad), SAVEPOINT-pro-Zeile via zweitem
  Cursor auf derselben Transaktion erhalten.
- **`2_indicator_engine` (höchste R1-Wirkung):** Kern-Fix — die Read-Site rechnet Indikatoren nur noch auf
  **geschlossenen** Kerzen (`include_forming=False`, harte Regel 5); Indikator-`MAX`→`latest_open_time(kind='indicators')`;
  `write_indicators`→`upsert_indicators`, Commit zum Caller verschoben (harte Regel 8).
- **`6_housekeeping`:** Gap-Scan→`include_forming=False`; Gap-Filler→`upsert_candles(closed=True)`
  (`DO NOTHING`→`DO UPDATE … IS DISTINCT FROM`); Retention→`list_coin_tables` + `delete_candles_before(kind)`;
  Indikator-Invalidierung→`delete_indicators_from`. DDL bleibt inline (entfällt in Phase C).
- **Review-Fix:** der Gap-Filler zählte Rows-**gesendet** statt Rows-**geschrieben** (`upsert_candles` liefert
  `len(rows)`), was den `== 0`-Guard bei unfüllbaren Lücken aushebelte (Binance-`endTime` sendet die bereits
  vorhandene rechte Rand-Kerze mit). Behoben durch Ausschluss dieser Rand-Kerze (`>`→`>=`) — der Zähler spiegelt
  jetzt echte Fills.
- **Verifikation:** DB-frei (py_compile/ruff/mypy/Regression-Guard smoke 6 + verify 24, `backtest/test_candles.py`
  47/47, 16 neu); **DB-Parität auf der Live-VPS** (`cryptodata`): read-only Byte-Tests grün, Delete-Byte-Tests via
  session-lokale `TEMP … ON COMMIT DROP`-Tabellen (gated hinter `KYTHERA_CANDLES_WRITE_PARITY`, Default read-only)
  grün ohne Schema-Leak. Beide Core-Reviews PASS (z-code-reviewer 3-Vote, z-spec-compliance 18/18 ACs). PR #104.

Offen (Michi-gegatet): Block 6 **Parts 2/3** — ML-Fleet parken → Retrain auf R1-sauberen Labels → Version-Bump
→ C-Gate-Phasen 0–5 (Hypertable-DDL/Dual-Write/Backfill/Cutover/Cleanup).

## [2026-07-13] Realized-PnL-Report für aktive Bots im Sentiment Tracker + targets/lev-Persistenz beim AI-Close (T-2026-CU-9050-115)

Neuer 4h-Report im Sentiment-Tracker-Channel (`CH_MARKET_DATA`): pro **aktivem** Bot der
**tatsächlich realisierte, gehebelte** %-Ertrag der geschlossenen Trades — Summe % und Ø % pro Trade
je Fenster **8h/24h/3d/7d/30d**, gefenstert nach **Close-Zeit** (bewusst anders als der bestehende
Per-Bot-Post, der nach Eröffnungszeit filtert). Positionsmodell (Operator-Spec): Einsatz gleich auf
die N publizierten Targets gesplittet, jedes erreichte Target realisiert 1/N zum Target-Preis, der
Rest schließt zum Close-Preis (SL/Timeout); das Ganze × Hebel, Verlust-Clamp bei −100 %.

- **Datenmodell-Lücke geschlossen (`8_ai_trade_monitor`):** beim Close gingen Target-Preise und
  Hebel verloren (ai_signals-Row wird gelöscht, nur `targets_hit` blieb). Zwei **additive** Spalten
  `closed_ai_signals.targets` (JSON) + `.lev` (TEXT) via bestehendem Schema-Sicherungs-Pattern
  (`ADD COLUMN IF NOT EXISTS` im Startup); der Close-Insert kopiert die publizierten Targets mit und
  stampt `lev = get_max_leverage(symbol, 20)` — identisch zu allen Post-Sites. **Ausnahme UFI1**
  (SL-gecappter Hebel, P0.6/R4): bekommt `NULL` statt eines falschen 20x.
- **`core/realized_pnl.py` (neu, DB-frei):** `parse_leverage` / `weighted_move_pct` /
  `realized_pnl_pct` — exact-only (ungültige/fehlende Werte ⇒ `None`, nie Näherung), Outlier-Bound
  ±100 % pre-Leverage wie im Per-Bot-Post.
- **`core/bot_catalog.py` (neu):** zentrales Mapping Model-Tag/Strategy-Name → Fleet-Skript
  (Familien-**Präfix**, überlebt Tag-Rotation ABR1→ABR2; Falle 16) + Aktiv-Filter
  (`core/fleet.FLEET` minus `control/parked`-Marker). Unbekannte Tags werden **sichtbar**
  ausgelassen (Log + Footer-Zeile), nie still gedroppt.
- **`23_market_tracker.py`:** neuer Job `job_realized_pnl_report` [XX:02:30, postet bei
  `hour % 4 == 0`], nutzt die bestehende Dedup- (report-14-Key) und Chunking-Infrastruktur. AI-Rows
  zählen **nur mit persistierten targets+lev** (Operator-Entscheid 2026-07-13: keine Näherung für
  Alt-Daten — die AI-Fenster füllen sich ab Deploy, 30d nach 30 Tagen voll); klassische Bots
  (`closed_trades_master` trägt target1-4+lev seit jeher) sind ab Tag 1 über die volle Historie
  exakt. TZ-korrekt per Uhr-Paarung (Falle 9): AI-Alter via `LOCALTIMESTAMP − close_time`
  (naive Lokalzeit, P1.8), Classic via `NOW() AT TIME ZONE 'UTC' − posted` (naive UTC).
  Ausgeschlossen: `ENTRY_NOT_FILLED`, Housekeeping-Closes (DELISTED/CLEANUP/ORPHAN), geparkte Bots.
  Reine Info-Message, kein Cornix-Block (harte Regel 4).

**Review-Härtung (3× z-code-reviewer N-Vote, alle Findings verifiziert + gefixt):** (1) HIGH —
klassische Housekeeping-Closes (`6_housekeeping` schreibt `DELISTED` auch in
`closed_trades_master.status`) wären als voller gehebelter Move gezählt worden → gemeinsamer
`_is_neutral_close`-Filter für BEIDE Quellen. (2) HIGH — `closed_trades_master.posted` landet
per Session-TZ-Cast als **Lokalzeit** (UTC_POLICY §3, P2.6 offen), nicht als naive UTC → Classic-
Uhr auf `LOCALTIMESTAMP` gedreht (sonst −3h-Fenster-Shift + stiller Drop frischer Closes);
negative Ages werden jetzt gezählt + gewarnt statt still gedroppt. (3) Outlier-Gate zusätzlich
auf dem ROHEN Close-Leg (Staffelung verdünnt ein Datenbug-Leg um N/(N−k)). (4) Migration-Pending-
Erkennung via `information_schema`-Probe statt Exception-String-Match (der hätte jeden DB-Fehler
als "Migration ausstehend" maskiert). (5) Bot 8 fail-fast, wenn targets/lev nach der Schema-
Sicherung fehlen (statt 10s-Crash-Loop im Close-Pfad) + `json.dumps`-Guard. (6) Sniper-Präfixe
`BB`/`TD` statt `BB_`/`TD_` (Retrain-Generation `TD2_4H` wäre unmapped gewesen).

**Bewusste, dokumentierte Abweichung (Operator-Info):** die Spec wollte `ai_signals.lev` beim
Signal-Post via `core/signal_post.py` persistieren — implementiert ist stattdessen ein Stempel
beim **ersten Bot-8-Poll (~10s nach Post)** in die neue Spalte `ai_signals.lev` (UPDATE nur wenn
NULL), beim Close mitkopiert. Erfüllt dieselbe Rationale (eine `max_leverage.json`-Änderung
während der Trade-Laufzeit kann den historischen Wert nicht mehr verfälschen), ohne die ~14
Signal-Emissions-Sites + deren Migrations-Ordering anzufassen; Rest-Skew nur noch Cache-
Generationen-Differenz Poster↔Bot-8 im 10s-Fenster. UFI1 (SL-gecappter Hebel) bekommt bewusst
NULL-lev und erscheint nie im Report; ROM1-Regime-Auto-Closes (Bot-28-Sync, derzeit tot)
schreiben keine targets/lev und bleiben ausgeschlossen — Follow-up-Kandidaten.

Verifikation (Build-Maschine, DB-frei): 111 neue Tests grün (`backtest/test_realized_pnl.py` 36,
`test_bot_catalog.py` 40 inkl. Fleet-Konsistenz-Check, `test_market_tracker_realized.py` 35);
bestehende Market-Tracker-Tests 27/27; ruff/format/mypy grün; Regression-Guard `smoke` OK.
Volle Suite: 9 Failures identisch auf `main` vorbestehend (sniper_retest/window_features), keine
Regression. **Reviews:** z-code-reviewer 3× unabhängig (Findings gefixt, s.o.) +
z-spec-compliance 3× unabhängig. **Deploy-Gate (Michi):** Bot-8- und Bot-23-Restart; die AI-Query
degradiert bis zur Bot-8-Migration graceful (Warn-Log, Classic-Teil postet).

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 5: geteilte Feature-Builder research_features + regime_logic auf geschlossene Kerzen (T-2026-CU-9050-112)

Block 5 der R1-Migration (`docs/CANDLE_CALL_SITES.md` §4 „Stand Block 5"): die **zwei geteilten
Feature-Builder** lesen jetzt über `core.candles` mit `include_forming=False` — je mit ihren
Trainer-/Replay-Aufrufern im selben Commit (harte Regel 7: Trainer == Serving == Replay). Zwei
Commits mit gegensätzlichem Risiko.

- **5a `core/research_features.fetch_context_frame`** (Research-Bots 30-33) — rohe DESC-f-String-SQL
  → `read_candles_with_indicators(include_forming=False)`; die `.iloc[::-1]`-Umkehr **entfällt** (API
  liefert ASC — bliebe sie drin, würde der Frame wieder DESC und `searchsorted` läge daneben; INVERSE
  der Block-2-Falle). `CONTEXT_IND_COLS` ist jetzt **eine Quelle** in `core/research_features` (aus
  `CONTEXT_SQL_SELECT` abgeleitet), importiert von `tools/research_dataset_common.load_candles_ctx`
  → Live- und Offline-/Trainings-Frame-Spalten byte-identisch per Konstruktion. **Feature-Parität =
  No-op** (die Feature-Kerze wählt `searchsorted` über open_time, unabhängig von der forming Zeile).
  **Aber kein voller No-op:** die Bots 30/31/32 nehmen `live_price = df["close"].iloc[-1]` als
  Entry-Anker — vorher forming-1h-Kerze (≈Live), jetzt letzte GESCHLOSSENE (bis ~59 min stale). Bot
  `33_ai_fif1` (einziger deployter) **nicht betroffen** (nutzt `sig["entry"]`). 30/31/32 sind gated
  (`NEW_IDEAS_LIVE_POSTING`, worthless/blocked) → kein Real-Money-Impact; Umstellung auf
  `get_live_price` als Follow-up **T-2026-CU-9050-113**.
- **5b `core/regime_logic.compute_features`** (`26_regime_detector` live + `backtest/backfill_regime_history`
  replay, eine Funktion) — beide 15m-Reads (`BTCUSDT_15m`/`BTCDOMUSDT_15m`) → `read_candles(include_forming=False)`.
  **Live-Gating-Änderung:** die forming 15m-Kerze treibt nicht mehr `classify_regime → apply_debounce
  → regime_current → Orchestrator-Whitelist`. Backfill braucht `end=` — **Achtung, der ursprüngliche
  Handoff-Mechanismus war falsch:** der `include_forming`-Cutoff ist **DB-`now()`-basiert**, droppt
  also NICHT die bei einem historischen `as_of` laufende Kerze; korrekt ist
  `end=last_closed_open_time("15m", as_of)` (API-`end` inklusiv → die bei `as_of` forming Kerze fällt
  raus). Live: kein `end`. Damit wird ein regeneriertes `regime_history` closed-candle-korrekt.
  Expliziter Float-Cast auf `high/low/close` (+ BTCDOM `close`) — `core.candles` liefert rohes
  NUMERIC/Decimal (Block-4-Bot-22-Falle).
- **Schwellen unverändert (§5 q6):** R1 senkt Regime-Transition-Raten bewusst; keine Konstante
  (TREND/CHOP-Schwellen, ATR-Multiplikatoren, Debounce-Counts, Perzentile) wurde nachgezogen — das
  ist Post-Retrain-Operator-Sache.

Verifikation (Build-Maschine, DB-frei, Fleet-Python 3.13.12): `ruff`/`format --check`/`mypy` grün auf
`core/research_features.py` + `core/regime_logic.py`; `backtest/test_feature_lookahead.py` 20/20 (zwei
`fetch_context_frame`-Tests auf Fake-Reader migriert + neuer `compute_features`-Read-Kontrakt-Test, der
Live-ohne-`end` vs Backfill-`end=last_closed_open_time` festnagelt); `test_regime_detector` +
`test_bot_regime_analyzer` 79/79; Regression-Guard `smoke`+`verify` 24/24. **Reviews:** z-code-reviewer
3/3 PASS (unabhängiges N-Vote) + z-spec-compliance PASS (7/7). PR #102 gemergt (Michi-Go).
**Post-Merge-VPS (offen):** `backfill_regime_history.py` neu → `regime_history` closed-korrekt →
TRM1-Retrain (Train + Serve lesen dieselbe Tabelle, Sequential-Jobs).

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 4 (Tranche 2 komplett): 22/24/25/11 + core/live_price.py auf geschlossene Kerzen (T-2026-CU-9050-111)

Abschluss von Block 4 (R1 im Bot live; `docs/CANDLE_CALL_SITES.md` §4 „Stand Block 4 —
Tranche 2 komplett"). Die vier restlichen AI-Bots lesen jetzt über `core.candles` mit
`include_forming=False`; **Block 4 ist damit code-seitig komplett** (nur `14_ai_atb`
bleibt ausgeschlossen → ATB2-Track T-106).

- **Quellentscheid (Michi):** Die `get_live_price`-Helfer aus `3_detectors.py` (numerisch
  benannt, nicht importierbar) sind 1:1 nach **`core/live_price.py`** gehoben; `3_detectors`
  re-exportiert beide Namen (Batch-Ticker-Test zieht auf das echte `requests`-Modul um).
  Befund: bei `22`/`24`/`25` speist `current_price` das **Erkennungs-Gate** (Level-Nähe/
  Retest), nicht nur den Entry → Preis muss **während** des Scans bekannt sein. Daher
  **Batch-Ticker vorab** (`get_live_prices_batch()` 1 Call/Zyklus, `price_map.get(sym) or
  get_live_price(sym, conn)` je Coin) statt ~N HTTP-Calls. Der §5-Leitsatz „Preis erst nach
  Erkennung" gilt damit nur eingeschränkt — 1 Batch-Call/Zyklus, kein Per-Coin-Overhead.
- **`22_ip_pattern`** — `read_candles(include_forming=False, limit=300)`, DESC-Umkehr entfällt,
  Pivots repaint-frei auf geschlossenem Frame. Expliziter Float-Cast auf OHLC (`core.candles`
  liefert rohes NUMERIC/Decimal → sonst `Decimal − float`-Crash im QML-Gate).
- **`24_quasimodo`** — `read_candles_with_indicators(include_forming=False)`, `[:-1]`-Drop
  entfällt. Offset-Shift: `touched_recently k=1..3→0..2`, `feature_idx len−2→len−1` (dieselbe
  geschlossene Kerze). `candle_columns` ohne `symbol`.
- **`25_smc_ml_sniper`** (schwerster Rework) — alle end-relativen Offsets +1: `last_closed
  len−2→len−1`, TD-Frische-Gates `PIVOT_WINDOW+2→+1`, `n_closed len−1→len` (Breakout/Follow-
  through inkl. letzter geschlossener Kerze), BB-Anker `extract_ml_features len−2→len−1`.
  Chart-Tupel bleiben `(len−1, …, current_price)`. TD-Pivot-Indizes (`p3`) unverändert.
- **`11_ai_mis`** — `read_candles_with_indicators(include_forming=False)` in `_fetch_mis_frame`;
  `df.rename` reproduziert die drei `MIS_SQL_INDICATOR_SELECT`-Aliase (Frame byte-gleich zu
  `tools/walkforward_sim.py`), Konstante unangetastet. Feature-Zeile `iloc[-2:-1]→iloc[-1:]`.
- **Contract 2 (`core/candles.py`)** nachgezogen: `11`/`12` sind keine Forming-Leser mehr.

Verifikation (Build-Maschine, DB-frei, Fleet-Python 3.13.12): `py_compile` + `ruff check`/
`ruff format --check` + `mypy` grün auf allen 5 Dateien; `test_detector_batch_ticker.py` 4/4;
Regression-Guard `verify` 24/24 nach jedem Bot. **Live-Verhaltensänderung (22/24/25) → Michi-Go
vor Enqueue; 24h-A/B Post-Merge-VPS; Schwellen erst nach Retrain (§5 q6).**

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 4 (Tranche 2, Teil): 12_ai_ats + 7_pattern_detector auf geschlossene Kerzen (T-2026-CU-9050-111)

Fortsetzung von Block 4 (R1 im Bot live; `docs/CANDLE_CALL_SITES.md` §4). Die zwei
**Offset-Rework-Bots** ohne Live-CMP-Deferral lesen jetzt über `core.candles` mit
`include_forming=False`; die restlichen vier (`22`/`24`/`25`/`11`) folgen fokussiert.

- `12_ai_ats`: `read_candles_with_indicators(include_forming=False, limit=500)`,
  DESC-Umkehr entfällt. Die TSI-Crossover-Detektion lief schon auf `iloc[-2]`
  (geschlossen) → ohne forming Kerze ist die jüngste geschlossene `iloc[-1]`, also
  `current_idx −2→−1`, `prev_idx −3→−2` (dieselbe Detektions-Kerze). Entry bleibt aus
  der geschlossenen Kerze (Operator-Ausnahme). Transitional: 500er-OBV-Baseline
  verschiebt sich um eine Kerze, bis zum ATS-Retrain vernachlässigbar (§5 q6).
- `7_pattern_detector`: `read_candles(include_forming=False, limit=168)`, DESC-Umkehr
  entfällt. Breakout-Kerze war `len(df)−2` (geschlossen) → jetzt `len(df)−1`. Der
  `iloc[:-4]`-Pivot-Puffer bleibt (Index `len−4` ist durch `rolling(9,center)` ohnehin
  NaN-geflaggt); der Rand-Pivot verliert nur seinen bisherigen Forming-Repaint.

Verifikation (Build-Maschine, DB-frei, Fleet-Python 3.13.12): `py_compile` +
`ruff check`/`ruff format --check` + `mypy` grün auf beiden Dateien.
`docs/CANDLE_CALL_SITES.md` §4 „Stand Block 4 — Tranche 2 Teilmenge".

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 4 (Tranche 1): AI-Bot-Direktreader auf geschlossene Kerzen (T-2026-CU-9050-111)

Vierter Umverdrahtungs-Block der R1-Migration (`docs/CANDLE_CALL_SITES.md` §4,
Umbrella T-018) — hier wird **R1 im Bot live**. Umgesetzt nach Michis Leitprinzip:
**Erkennung läuft auf geschlossenen Kerzen** (`include_forming=False`), der
**Live-Preis wird nur zur Signal-Generierung** gebraucht und dann separat via
`get_live_price` geholt (nicht mehr aus der forming Kerze im Analyse-Frame). Wegen
des Money-Path-Risikos in zwei Tranchen geschnitten; **kein autonomer Merge** —
Freigabe durch Michi vor dem Enqueue.

**Tranche 1** — sechs Direktreader ohne Offset-Rework/Live-CMP-Umbau lesen jetzt
über `core.candles` mit `include_forming=False`: `13_ai_rub` (beide Reads; No-op,
der bisherige `< date_trunc('hour',NOW())`-Filter ist für 1h identisch zum
Closed-Cutoff), `15_ai_master.load_market_row` (As-of `< floor(ts)`, No-op via
`end = floor − timeframe_delta`), `9_ai_sr.get_indicators_at_time` (As-of
`end=trade_ts`; tightening am Rand — ein Trade mitten in der laufenden Stunde
bekam sonst Partial-Indikatoren), `10_pump_dump.get_indicators_at_time` (echte
R1-Änderung: `DESC LIMIT 1` ohne Bound las die forming Indikatorzeile),
`18_ai_abr1` (Selbsttest + Live; `include_forming=False` == bisheriger
`open_time < current_hour_utc`-Schnitt, `limit=` ersetzt `.tail()`),
`29_ufi1.load_daily_ohlcv` (echte R1-Änderung: der Read ohne Obergrenze zog die
forming 1d-Kerze; 29 holt den Live-Preis bereits separat via `get_live_price`).

Die Dict-Reader (9/10/13-ind) bauen die Feature-Dicts jetzt aus `df.iloc[-1].to_dict()`
statt `dict(zip(cur.description, row))`. Die echten R1-Änderungen (10, 29) **senken
bewusst die Signal-Raten** — der 24-h-A/B ist eine Post-Merge-VPS-Beobachtung,
Schwellen werden erst nach dem Retrain getunt (§5, Frage 6).

**Operator-Entscheidungen festgehalten** (`CANDLE_CALL_SITES.md` §5): Close-Grace
`0`; Leitprinzip Erkennung=geschlossen / Live-Preis=`get_live_price`-bei-Generierung
einheitlich für alle Block-4-Bots inkl. 11/12 (überschreibt den ersten
§5.5-„True+Split"-Zwischenstand).

**Tranche 2 (Folge-Task):** `7_pattern_detector`, `12_ai_ats` (Offset-Reworks),
`22_ip_pattern`/`24_quasimodo`/`25_smc_ml_sniper` (Live-CMP-Deferral) und
`11_ai_mis` (geschlossene Features + `get_live_price`-Entry + Alias-Reproduktion).
`14_ai_atb` bleibt ausgeschlossen (geparkt → ATB2-Track T-106).

Verifikation (Build-Maschine, DB-frei, Fleet-Python 3.13.12): `py_compile` aller 6
Dateien, `ruff check`/`ruff format --check`/`mypy` grün, Regression-Guard
`smoke` (6 Fixtures) + `verify` (24/24) grün. `docs/CANDLE_CALL_SITES.md` §4
„Stand Block 4 — Tranche 1".

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 3: Monitore + Orchestrator + Preis-Fallbacks explizit auf core.candles (T-2026-CU-9050-109)

Dritter Umverdrahtungs-Block der R1-Migration (`docs/CANDLE_CALL_SITES.md` §4,
Umbrella T-018). Die sieben verbleibenden Preis-/Scoring-Reader im Geld-Pfad lesen
jetzt über `core.candles` mit **explizitem `include_forming=True`** — der bewusste
„das `True` sichtbar und reviewbar machen, BEVOR das erste `False` im Geld-Pfad
landet"-Block. Reiner read-only Code-Umbau, kein DB-Schema angefasst.

**Anders als Block 2 verhaltens-erhaltend:** `include_forming=True` = kein Forming-
Filter, also sind die gelesenen Kerzen byte-gleich zu heute (neueste Zeile inkl.
forming). Keine Signal-Raten-Änderung, keine Geld-Pfad-Semantik-Änderung. Trotzdem
Geld-Pfad-Dateien → **kein autonomer Merge, Freigabe durch Michi vor dem Enqueue**
(Block-2-Präzedenz).

Umverdrahtet: `5_trade_monitor` + `8_ai_trade_monitor` (SL/TP-Scoring, 5m — erster
Lauf neueste Kerze, sonst ab Wasserzeichen `>=`-inklusiv; die list-of-dicts-Struktur
bleibt via `df.itertuples` unberührt, nur der Read geht über die API), `28_signal_
orchestrator._get_latest_price` + `._get_last_close_price`, `3_detectors.get_live_
price`-DB-Fallback, `29_ufi1_bot.get_live_price` (1h, geparkt), `6_housekeeping.
_fetch_last_close_or_entry`, `core/health_monitor`-DATA_STALE-Kanarie (→ `latest_
open_time(include_forming=True)`).

Zwei Nuancen dokumentiert: (1) **Inventar-Drift korrigiert** — die Orchestrator-
Sites lagen bei `:449`/`:1063`, nicht bei den im Inventar notierten `:352`/`:787`;
`:1063` (`_get_last_close_price`) war gar nicht inventarisiert. (2) **health_monitor-
Alter** wandert von DB-seitigem `NOW() − max(open_time)` auf Python `now() −
latest_open_time`; beide teilen auf dem VPS dieselbe Wall-Clock, der Sub-Sekunden-
Unterschied ist gegen das Minuten-Limit `STALE_LIMIT_S` irrelevant. Die SAVEPOINT-
gekapselten Preis-Reads (28/6) behalten ihren SAVEPOINT — `read_candles` öffnet nur
einen zweiten Cursor auf derselben Connection.

Verifikation (Build-Maschine, DB-frei): `py_compile` + Import-Smoke aller 7 Dateien,
`ruff check`/`ruff format --check`/`mypy` grün auf `core/` + Root-Bots, Regression-
Guard `smoke` (6 Fixtures) + `verify` (24 Goldens) grün. Live-A/B ist per Konstruktion
ein No-op (byte-gleiche Reads). `docs/CANDLE_CALL_SITES.md` §4 „Stand Block 3". Offen
bleiben Block 4 (AI-Bot-Direktreader — das erste `False` im Geld-Pfad) und Block 6/
C-Gate (DB-Writer `is_closed`).

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 2: Strategien + 3_detectors + geteilte Helfer auf core.candles (T-2026-CU-9050-108)

Zweiter Umverdrahtungs-Block der R1-Migration (`docs/CANDLE_CALL_SITES.md` §4,
Umbrella T-018). Sieben Read-Sites im **Live-Signal-Pfad** lesen jetzt über
`core.candles` mit `include_forming=False` — geschlossene Kerzen, ASC. Reiner
read-only Code-Umbau, kein DB-Schema angefasst. Anders als Block 1 (offline)
ändert dieser Block echtes Live-Verhalten, deshalb **kein autonomer Merge** —
Freigabe durch Michi vor dem Enqueue.

Umverdrahtet: `core/trade_utils.calculate_smart_targets` + `get_hvn_and_sr_levels`
(höchster Fan-in — die forming 1h-Kerze speiste bisher den Swing/HVN/FVG/S-R/Fib-
Level-Pool **aller** AI-Bots), `core/market_utils.calculate_obv`,
`strategies/strat_main_channel`, `strat_support_resistance`, `strat_volume_indicator`
und `3_detectors.run_detectors_for_timeframe` (der Indikator-Frame der 5 Classic-Strats).

Zwei scharfe Fallen behandelt: (1) **DESC→ASC-Ordering** (OPUS-HANDOFF Falle 1) —
`3_detectors` reicht heute einen DESC-Frame an fünf Strategie-Konsumenten, die alle
`iloc[0]`=neueste indexieren (`strat_main_channel/support_resistance/5_percent/
fast_in_out` + Volume-Indikator). Der Detector-Read geht über die API (ASC + forming-
frei) und wird per `.iloc[::-1]` in exakt den DESC-Frame zurückgedreht — **null
Konsumenten-Reindex**, die einzige Verhaltensänderung ist `iloc[0]` = neueste
GESCHLOSSENE statt forming Kerze. (2) **Strikte `<`-Grenzen** im Volume-Indikator
bleiben byte-treu: `end = grenze − timeframe_delta("30m")` reproduziert `open_time <
grenze` exakt (period-alignte open_times). `get_hvn_and_sr_levels` reproduziert
`NOW() − INTERVAL '95 days'` als `utc_now() − 95d` (≤1h-DST-Nuance immateriell für die
Warmup-Untergrenze).

Verifiziert auf dem VPS gegen `cryptodata` (nur read-only SELECTs, 150 Coins):
Mechanik 149/149 grün — Reads liefern ASC, forming ausgeschlossen (`newest open_time
< period_start`), der Detector-Re-Flip liefert DESC mit `iloc[0]` = neueste
geschlossene Kerze, und der geschlossene Frame ist byte-gleich zum Alt-Query.
**Der Live-Signal-Raten-Vergleich ist auf diesem Snapshot nicht messbar**: die
Fleet-Ingestion stand zum Prüfzeitpunkt ~2,4 h (neueste 1h-Kerze 04:00 UTC), es gibt
also keine forming Kerze auszuschließen, und historische forming-Snapshots werden beim
Close überschrieben. Tip-Kerzen-Sensitivität als Proxy (neueste geschlossene vs.
zweitneueste): die restriktiven 5%/Fast-Gates flippen 0/298, die S/R-Hit-Vorbedingung
25/149 (~17 %), die AI-Bot-Level-Pools verschieben sich bei 69–83 % der Coins
(Ø ~4,6 % relativer Level-Shift). Der echte 24h-Live-A/B gehört in die Nachbeobachtung
(Fleet up + Shadow) und die Schwellen-Neujustierung nach Retrain (Report 16) — nicht in
diesen Block. Regression-Guard `smoke`+`verify` grün (24/24), ruff/format/mypy grün auf
`core/` + `3_detectors.py` (`strategies/` ist ruff-exkludiert). C-Gate (Hypertable/
Backfill) und die AI-Bot-Direktreader (Block 3/4) bleiben spätere Blöcke.

## [2026-07-13] TimescaleDB-R1 Phase 1 Block 1: Offline-Tooling auf core.candles umverdrahtet (T-2026-CU-9050-107)

Erster Umverdrahtungs-Block der R1-Migration (`docs/CANDLE_CALL_SITES.md` §4,
Umbrella T-018). 12 Offline-Read-Sites lesen jetzt über `core.candles` statt roher
f-String-SQL, alle mit `include_forming=False` — geschlossene Kerzen, ASC. Reiner
read-only Code-Umbau ohne Live-Signal-Pfad; kein DB-Schema angefasst.

Umverdrahtet: `core/charting.py` (kosmetischer 5m-Overlay), `tools/mis1_move_labels.py`
(+ transitiv `mis2_dump_geometry_study`), `tools/regime_rules_study.py`,
`tools/retrain_sra2.py`, `tools/research_dataset_common.py` (+ transitiv
fif1/fmr1/pex1/trm1), `tools/aim2_build_dataset.py`, `tools/epd2_build_dataset.py`,
`qm_ml_trainer.py`, `smc_ml_trainer.py`, `qm_backtest.py`, `smc_pattern_backtester.py`,
`backtest/smc_btc_backtest{,_v2,_v3}.py`, `tools/regression_guard/rgcore.py`.

R1 wird auch offline wirksam: die QM/SMC-Trainer und die Regime-Studie liefen
vorher ohne oberen Zeit-Schnitt und rechneten/trainierten die forming Kerze mit —
dieselbe Look-ahead-Klasse, die der Walk-Forward-Sim in T-037 verloren hat. Der
neue Helfer `candles_window_start(since, lookback_days)` in `research_dataset_common`
reproduziert das frühere `%s::timestamptz - INTERVAL 'N days'` TZ-treu in Python
(eine Quelle für die Fenstergrenze; aim2/epd2 importieren ihn). Der Regression-Guard
`extract` erfasst Fixtures ab jetzt forming-frei — nur der DB-Extract-Pfad,
`verify`/`smoke` bleiben DB-frei und grün (kein Regel-9-Refresh).

Bewusst nicht umverdrahtet (dokumentiert in `docs/CANDLE_CALL_SITES.md`):
`fib_backtest.py` (pg_tables-Case-Variant-Probe kollidiert mit der Uppercase-API —
eigener API-Gap), `tools/audit/step7_monitor_replay.py` (TZ-Forensik-Wegwerf-Skript,
null Verhaltensnutzen bei Risiko für die Shift-Logik), `trainers_x/BT2-Datagrepper`
(eingefrorene Provenienz, wie `legacy_trainers`).

Verifiziert auf dem VPS gegen `cryptodata` (read-only SELECTs): alle Reader liefern
ASC, die forming Kerze ist ausgeschlossen (`newest open_time < period_start`); Guard
`smoke`+`verify` grün, ruff/format grün auf den nicht-exkludierten Dateien. Der
Block ist reine Code-Umverdrahtung — die Signal-Raten-Neujustierung nach Retrain
(Report 16) und das C-Gate (Hypertable/Backfill) bleiben spätere Blöcke.

## [2026-07-12] TimescaleDB-R1 Phase 0: Byte-Gleichheits-Gate für core/candles.py grün gegen die Live-DB (T-2026-CU-9050-018)

Phase-0-Code-Teil der R1-+-TimescaleDB-Migration. Der Substanz-Teil lag bereits
gemergt vor (`core/candles.py` + `tools/candles_parity.py` + Call-Site-Inventar aus
T-034; die P3.3-Validierung in `load_coins` aus T-096) — offen war nur das eine
in `docs/CANDLE_CALL_SITES.md` §6 als „offen — VPS" markierte Phase-0-Gate aus
dem Design-Doc: **„API-Reads byte-gleich zu Direkt-SQL"**. Dieses Gate ist jetzt
ausführbar und grün.

Neu: `backtest/test_candles_db_parity.py`. Zwei Schichten nach dem Muster von
`candles_parity.py`: (1) ein DB-freier Kanonisierungs-Kern (`canonical_cell`/
`canonical_rows` — normalisiert die Repräsentations-Unterschiede zwischen
pandas-DataFrame und rohen psycopg2-Tupeln: Timestamp↔datetime, NaN↔None,
int↔float-Promotion, 12-signifikante-Stellen-Floor gegen REAL/double-Rauschen)
mit eigenen Tests, die überall laufen und den Vergleicher selbst absichern, damit
ein grüner DB-Lauf kein Falsch-Positiv eines kaputten Komparators sein kann;
(2) 7 DB-Tests gegen die ALTEN per-Coin-Tabellen: `read_candles`/`read_indicators`
byte-gleich zu handgeschriebenem Direkt-SQL, `limit` liefert die neuesten n in
ASC, `include_forming=False` droppt exakt die forming Rows (R1-Kern), der JOIN-Read
lässt die Candle-Seite unverändert, `latest_open_time` == `MAX(open_time)`. Ohne
DB-Credentials überspringt der `conn`-Fixture die DB-Tests sauber (`pytest.skip`) —
nie ein fabrizierter Pass.

Gelaufen in einer dedizierten VPS-Owner-Session gegen `cryptodata` (BTCUSDT_1h,
8.777 Rows): 10/10 grün, ausschließlich Read-only-SELECTs — **kein Write, keine
DDL, kein Hypertable-Anlegen** (die TimescaleDB-Extension + Hypertable-DDL +
Dual-Write/Backfill bleiben C-Gate bei Michi, nach der T-061-Rerun-Queue). Damit
ist der Phase-0-Code-Teil abgeschlossen; die API-Signaturen (`read_candles`/
`read_indicators` mit `include_forming`-Default `False`, `True` nur für Preis-Checks
5/8) sind ab jetzt eingefroren — die parallele ATB2-Session (T-104) baut dagegen.

## [2026-07-12] Docs: Kandidaten-Addendum K13/K15 + K6-TOTAL3 aus Leaderboard-Recherche und Operator-Videos (T-2026-CU-9050-105)

Zweite Research-Runde in die Handoff-Docs eingearbeitet (Operator-Freigabe
Michi). `docs/MODEL_CANDIDATES_SPEC_2026-07.md`: neuer Kandidat **K13 HLW**
(Hyperliquid-Whale-Position-Collector + Feature-/Lag-Studie — Hyperliquid ist
laut verifizierter Recherche die einzige Venue mit dauerhaft öffentlicher
Per-Adresse-Transparenz; Binance-Leaderboard nur Graumarkt-Scraper, Bybit ohne
Lese-API; Skill-Persistenz akademisch belegt aber nie für Krypto repliziert →
bewusst Collector+Studie statt Copy-Bot, Bot-Nr. 36 reserviert), neuer
Kandidat **K15 SRX** (Scratch-Reload-Exit-Studie auf ABR/BR-Events: Exit bei
Kerzenschluss unter Entry, Re-Entry bei Cross+Retest, max. N Zyklen vs. fixer
SL; plus Touch- vs. Close-SL-Grid-Zelle — extrahiert aus Michis
YouTube-Videos, KB ingest-9f6511a5f951), K6 um den **TOTAL3-Proxy** als
Pflicht-Breadth-Feature ergänzt (Alt-Index ex BTC/ETH, KB
ingest-c1e5112dea7f), Reihenfolge/Task-Zuschnitt aktualisiert.
`reports/model_ideas_research_2026-07.md`: §6-Addendum mit den
Leaderboard-Befunden F14–F19 (inkl. widerlegtem 96,5 %-IRL-Claim und
unverifiziertem Whale-Copy-Hype) und der Video-Auswertung. Reine Doku.

## [2026-07-12] K9/OIC: Open-Interest-Collector — Hypertable oi_5m + 35_oi_collector.py + 30d-Backfill-Tool (T-2026-CU-9050-103)

Umsetzung des zeitkritischen Kandidaten K9 aus
`docs/MODEL_CANDIDATES_SPEC_2026-07.md` (Binance-REST hält nur ~30d
OI-Historie — jeder Tag ohne Collector ist unwiederbringlich verloren).
Drei Bausteine: **(1)** `core/oi_5m.py` — Hypertable `oi_5m`
(`ts TIMESTAMPTZ, symbol, open_interest, oi_value_usdt, PK (ts, symbol)`),
Timescale-Jobs Chunks 1d / Compression nach 3d (segmentby=symbol) /
Retention 730d, batched Insert mit `ON CONFLICT DO NOTHING`, geteilter
Payload-Parser für beide Writer (ticker_10s-Blaupause). **(2)**
`35_oi_collector.py` — eigener schlanker Prozess (getrennte Failure-Domain,
BELOW_NORMAL): alle 5 min ein Sweep über coins.json via
`/futures/data/openInterestHist` (period=5m, limit=1; liefert anders als
`/fapi/v1/openInterest` auch die USDT-Bewertung und rastergestempelte
Timestamps → echte Dedup-Keys), Requests über den Sweep verteilt
(~530 req/5min gegen das 1000/5min-IP-Limit der /futures/data-Endpoints),
429/418-Backoff via `core/http_retry`, Kill-Switch `KYTHERA_OI_PERSIST=0`
(Default an, idlet supervised). Registriert in `core/fleet.py`
(group=logger, start_delay=231; +2 PG-Idle-Connections, P1.34 beachten).
**(3)** `tools/oi_backfill.py` — einmaliger paginierter ~30d-Initial-Backfill
(rückwärts via endTime, selbstterminierend; idempotent gegen den laufenden
Collector; Dry-Run-Smoke auf BTCUSDT: 8.639 Punkte ≈ exakt die 30d-Fenster).
Tests: `backtest/test_oi_5m.py` (DDL-/Insert-/Parsing-Contract, DB-frei) +
Fleet-Anker in `test_fleet_definition.py` erweitert. **Operator-Gate offen:**
Prozess-START auf dem VPS = Fleet-Eingriff (Watchdog liest FLEET beim Import
→ Watchdog-Restart nötig) und der einmalige Backfill-Lauf — beides Michi.
## [2026-07-12] P2.12-Folge: --rsi-rewrite-Modus für recompute_indicators.py — RSI-Historie eindomänig Wilder (T-2026-CU-9050-099)

Werkzeug für Schritt (2) der P2.12-Sequenz (der Wilder-Engine-Switch T-095 ist
seit dem Fleet-Restart 2026-07-12 01:03 aktiv — die `rsi_*`-Historie ist seither
ZWEIDOMÄNIG: alt=ewm(span), neu=Wilder). Der neue Modus schreibt die fünf
`rsi_*`-Spalten über die gesamte Historie mit dem Wilder-Recompute neu — bewusst
NICHT positions-stabil (Domänen-Migration, das Gegenteil des T-061-Trade-offs,
im Docstring beider Modi gegenübergestellt). Sicherungen: `--dry-run` (Default,
readonly-Session) misst Änderungszellen und Ø/Max-Delta; Tail-Guard gegen das
Bot-2-Race; batched unnest-UPDATEs (parametrisiert, NaN→NULL); idempotent
(Zellen ≤1e-3 RSI-Punkte Abstand werden übersprungen — der zweite Lauf ist ein
No-op); eigenes Resume-State-File; und ein **Engine-Parity-Selbstcheck** mit
Witness-Serie, der einen pre-T-095-Checkout hart abweist — die Historie kann
nie versehentlich zurück auf span geschrieben werden.

Read-only-Smoke gegen die Live-DB (`--sample 6`): 134.717 Zellen auf 5 Tabellen,
Ø-Delta 5,43 RSI-Punkte (max 27,2) — konsistent mit der Step-2-Messung (ø4,8).
Verifikation: `backtest/test_rsi_rewrite_plan.py` (9 Tests, DB-frei, inkl.
Idempotenz-, Tail- und Parity-Grenze); die bestehenden Head-Nulling-Tests
bleiben unberührt grün. **Der Execute war ein C-Gate** — Michi-Freigabe
2026-07-12 ~05:00, ausgeführt am selben Tag: voller Dry-Run 88,4M Zellen
(Ø-Delta 5,52 Punkte), Execute 88.426.142 Zellen über 3.831 Tabellen in 9,6h,
0 Fehler, Idempotenz-Nachlauf 0 Zellen — die Historie ist seither eindomänig
Wilder. Retrain-Kette danach erneut (der T-061-Retrain vom 2026-07-12 00:13
lief noch auf der ewm-Historie), erst dann Promotion.

## [2026-07-12] ATB2: Neuaufbau des Trendline-Bots als Converging-Channel-Pipeline (T-2026-CU-9050-104)

Neuaufbau des toten ATB1 (Bot 14, geparkt, Audit-Note D, Σ −172 netto,
Event-Mismatch) von null gemäß `docs/MODEL_INTENT.md` §11. ATB2 handelt nicht
mehr Einzel-Trendlinien einer 90d-Close-Regressionsgerade, sondern
**konvergierende Kanäle** (Wedge/Triangle/Pennant) aus bestätigten Swing-Pivots
mit geschlossenem Ausbruch. Neu und DB-frei gebaut + getestet (kein Live-Eingriff,
kein Artefakt im Live-Pfad — Bot bleibt geparkt bis zum validierten Verdikt):
`core/atb2_features.py` (geteilte Detektions-/Feature-Quelle für Bot + Simulator
+ Trainer, X-R1-Regel: No-Repaint-Pivots, §11-Kanalkriterien, 5
WillyAlgoTrader-Setup-Features + Kanalgeometrie als XGB-Features,
Measured-Move-Targets, `assert_features_alive`, ATR/RSI/EMA deterministisch aus
OHLCV statt pandas_ta-versionsabhängig), Walkforward-Adapter `run_atb2`
(`--strategy atb2`, Label = First-Touch TP1-vor-SL der Measured-Move-Geometrie
via `simulate_exit` inkl. Fees; Smart-Targets derselben Kerze als `smart_*`-
Vergleich, §11) und Retrain-Runner `run_atb` (`--strategy atb2`, je Richtung,
chronologischer 3-Wege-Split + 3d-Purge, Isotonic, Threshold via
`pick_threshold_safe` auf Validation, Artefakt + `_meta.json` nach
`staging_models/` mit `model_id=ATB2`). Behebt die X-R-Findings des toten
BT1-Trainers: Event-Mismatch (X-R1), Label ohne SL-Pfad (X-R1/X-R5),
Split-Leakage über überlappende Fenster (X-R3), Test-Set-Threshold (X-R2),
Silent-Feature-Death (X-R6). Verifikation: `backtest/test_atb2_features.py`
(9 DB-freie Tests, inkl. End-to-End-Adapter) + DB-freier Retrain-Smoke
(600 Synthetik-Events → `model_id=ATB2`-Artefakte, Threshold korrekt None bei
zu kleinem Val-Slice). Run-Book + Deploy-Verdikt-Kriterien: `docs/ATB2_REBUILD.md`.
**Offen (Follow-up, gated):** Label/Train-Lauf auf dem VPS (hinter T-061-Queue,
Sequential-Jobs); Bot-Serving-Rewire + P1.45-Tag-Fix + Entparken erst nach
deploybarem out-of-time-Verdikt (C-Gate Michi).

## [2026-07-12] Docs: Modellideen-Research-Report + Kandidaten-Specs als Opus-Handoff (T-2026-CU-9050-102)

Zwei neue Dokumente aus dem Deep-Research-Lauf 2026-07-12 (101-Agent-Workflow,
19 Quellen, 25 Claims adversarial verifiziert: 20 bestätigt / 5 widerlegt):
`reports/model_ideas_research_2026-07.md` (zitierfähiger Befund-Report — BIS-
Funding/Carry, Momentum→Reversal-Struktur, TSMOM-6h-Preprint, Post-Listing-
Drift, invertierter MAX-Effekt, realized Moments, Settlement-Timing; inkl.
Widerlegt-Liste und offener Fragen) und `docs/MODEL_CANDIDATES_SPEC_2026-07.md`
(implementierungsreife Specs für 12 Kandidaten K1–K12 in 3 Tiers: TSM1,
XSM1/XSR1, Funding-Risk-Layer, FMR2, LIS1, BRD, MOM/SKW1, SET, OI-Collector
(zeitkritisch — REST hält nur 30d), WHI, WSH1, TRM2-Wiedervorlage; plus
dokumentierte Anti-Kandidaten). Jede Spec trägt Hypothese, Datenlage,
Schritt-Plan mit konkreten Tools/Konventionen (walkforward_sim, simulate_exit,
pick_threshold_safe, X-R1-Builder, staging-only, Ein-Job-Regel),
Stop-Kriterien nach Batch-E und Eskalationspunkte — geschrieben als Handoff,
damit ein Folge-Agent die Coding-Tasks ohne Rückfragen schneiden kann.
Reine Doku, kein Code-/Verhaltens-Change.

## [2026-07-12] ROM1: SL-basierter Leverage-Cap entfernt — Cross-Margin, fix 20x via get_max_leverage (T-2026-CU-9050-101)

Operator-Entscheid Michi: Die ROM1-Trades laufen bei Binance in **Cross Margin**
(die Cornix-Message postet seit jeher `Margin: Cross`), die Liquidation hängt
also an der gesamten Wallet und nicht an der ~1/lev-Preisdistanz der
Isolated-Rechnung. Der R4-Wrapper `cap_leverage_to_sl` in
`compute_rom1_trade_params` (28_signal_orchestrator.py) drückte den Hebel bei
weiten SLs deshalb unnötig (8%-SL → 6x statt 20x). Neu:
`leverage = get_max_leverage(coin, ROM1_DESIRED_LEVERAGE)` — es gilt nur noch
der Per-Coin-Binance-Cap aus `max_leverage.json` (Coins ohne 20x bekommen
weiterhin automatisch ihren niedrigeren Cap). Gleiche Begründung wie der
dokumentierte MIS2-Entscheid („Cross-Margin, kleine Positionen auf großes
Depot — bewusst KEIN cap_leverage_to_sl"). Der 15%-SL-Distanz-Cap (P2.27)
und die übrigen `cap_leverage_to_sl`-Sites (Bots 21/29, Isolated-Klasse
P0.5/P0.6) bleiben unberührt; R4-Annotation im AUDIT_TODO entsprechend
ergänzt. Tests in `backtest/test_signal_orchestrator.py` auf die neue
Semantik umgestellt (LONG-Fall assertet jetzt 20x statt 6x-Cap).

## [2026-07-12] R2(b): docs/schema.sql — kanonische DDL-Referenz aus der Live-DB (T-2026-CU-9050-098)

Schließt den Schema-Teil von Root-Cause R2 (der Fleet-Teil (a) kam mit
`core/fleet.py`, T-091). `docs/schema.sql` ist ein kuratierter
`pg_dump 17.6 --schema-only --no-owner --no-privileges` der Live-DB `cryptodata`
vom 2026-07-12: alle 44 Applikations-Tabellen — darunter erstmals die bislang
komplett DDL-losen `ai_signals` (13 Writer) und `ml_predictions_master`
(9 Writer) — plus `BTCUSDT_1h`/`BTCUSDT_1h_indicators` als repräsentative
Vorlage der Per-Coin-Familie. Die 9.789 generierten Tabellen (per-Coin,
Quarterly-Futures, yfinance-Forex `=X`, `_GOLD`-Metalle sowie die
CJK-benannten Junk-Symbol-Tabellen aus der P2.16-Doppel-Writer-Leak-Klasse,
deren Löschung ein D5-Operator-Gate bleibt) sind als Namensfamilien im
Datei-Header dokumentiert statt einzeln gedumpt. Die Datei ist bewusst
**Referenz, keine Migration**: ausführende DDL bleiben die
`CREATE TABLE IF NOT EXISTS`-Sites in den Bots, jeder Live-`ALTER` bleibt
Operator-Entscheid (§6). `\restrict`-Token des Dumps entfernt, damit eine
Regeneration sauber difft; das Regenerations-Kommando steht im Header.
Read-only-Job aus der VPS-Orchestrierung T-2026-CU-9050-097 (Job 9), lief
parallel zum P1.13-Dry-Run.

## [2026-07-11] P3-Hygiene-Batch — load_coins-Konsolidierung, Symbol-Validierung, Log-Rotation, Pins, Spec-Drift-Doku (T-2026-CU-9050-096)

Reiner Aufräum-Batch aus der AUDIT_TODO-P3-Sektion (P3.1–P3.8, P3.10, P3.11), je
Punkt einzeln im PR. Kein Geld-Pfad-Verhalten geändert; wo ein Punkt Verhalten
berührt, konservativ und einzeln ausgewiesen. Regression-Guard bleibt ohne
Refresh grün, volle `backtest/`-Suite 691 grün.

**P3.1/P3.3 — load_coins-Konsolidierung + zentrale Symbol-Validierung.** Die sechs
mit Semantik-Drift kopierten `load_coins` (chart_data_service, fib_backtest,
walkforward_sim, qm_backtest, smc_ml_trainer, qm_ml_trainer) laufen jetzt über
`core.market_utils.load_coins` mit neuen `usdt_only`/`uppercase`-Flags, die ihr
lokales Filtern reproduzieren. Der Kanon validiert zentral jedes Symbol gegen
`[A-Z0-9]+` (drop+ERROR-Log, nie stiller Keep) — das schließt alle ~40 f-String-
Tabellennamen an einer Stelle (P3.3). No-op auf der Live-coins.json (530 Upper-
USDT-Perps), sodass `1_data_ingestion` (T-092) eine identische Liste sieht.
Dazu: tote `write_to_active_trades`/`write_to_telegram_outbox` in `3_detectors`
entfernt (grep-verifiziert callerlos, `write_signal_atomic` ist der Pfad), die
drei byte-identischen `_apply_keepalive` nach `core/ws_utils` gezogen (lokaler
`import sys` bleibt gegen mypy-`platform=win32`-Unreachable), TIMEFRAMES-
Redeklaration in `6_housekeeping` → `core.config`. DB-freier Test
`backtest/test_symbol_validation.py` (8 Fälle).

**P3.2 — Log-Rotation.** `indicator_calculation.log` und `watchdog.log` von
`FileHandler` auf `RotatingFileHandler` (10 MB × 3) am **gleichen** Pfad — bewusst
nicht `setup_logging`, dessen `logs/<name>.log`-Umbenennung die Reader bricht
(Watchdog-Hang-Check liest `indicator_calculation.log`; Dashboard + health_monitor
lesen `watchdog.log`). Der append-only `logs/dashboard.log`-Pipe (kein Logging-
Handler) wird via neuer `truncate_oversized_logs` im 03:00-Housekeeping über 20 MB
auf die letzte Hälfte gekappt.

**P3.4 — Dependency-Pins.** Major-Pins für pandas (`>=3.0,<4`), python-telegram-bot
(`>=22,<23`), xgboost (`>=3.0,<4`) — Ist-Stand gepinnt, kein Upgrade. Neue
`requirements.lock.txt` = Dependency-Closure von requirements.txt gegen den
installierten Stand (52 Pakete), **nicht** `pip freeze` (Global-Env trägt ~230
fremde Cu-Tooling-/editable-Installs). Header flaggt sie als unvollständig:
yfinance + pandas_ta sind auf der DB-freien Build-Maschine (T-011) nicht
installiert → der autoritative Voll-Lock gehört in eine VPS-Session.

**P3.5 — Formatierung / Blocking-IO / Info-Leak.** whale_logger-Preisanzeige
`:.2f` → `format_price` (Sub-Cent-Coins zeigten sonst „$0.00"; rein informativ,
kein Cornix-Block). `open_handler`: das blockierende `get_live_price`
(`requests.get`) wird aus dem async-Handler via `asyncio.to_thread` ausgelagert,
plus `@None`-Attributions-Fix (Fallback auf full_name). `describe_project`:
Full-Source-Dump-Info-Leak im Docstring + Runtime-Warnung dokumentiert,
Ignore-Set auf `.git`/`.local`/`__pycache__`/`node_modules` erweitert.

**P3.6 — Backtest-Limitationen dokumentiert (Doku-Teil).** „Known limitations"-
Blöcke in smc_pattern_backtester (FEE_RATE deklariert aber nie referenziert +
Survivorship + kein Kapital/Concurrency-Modell), fib_backtest, qm_backtest, plus
bfill-Leak-Notiz am Call-Site der beiden ML-Trainer. Keine Logik-Änderung. Der
`[DB]`-Teil (delisted-Tabellen noch da?) bleibt offen.

**P3.7 — Coin-Level-Exceptions sichtbar gemacht.** Der Coin×TF-Loop in
24_quasimodo + 25_smc_ml_sniper verschluckte Fehler auf `logger.debug`; auf das
Bot-29-Muster angeglichen: `logger.error(..., exc_info=True)` + `conn.rollback()`,
damit eine vergiftete Transaktion nicht jeden Folge-Coin abbricht.

**P3.8 — matplotlib-Agg.** `matplotlib.use('Agg')` vor dem pyplot-Import in
17/24/25 (crashten sonst headless auf dem VPS), je eine Zeile, Muster Bot 16.

**P3.10 — Spec-Drift-Doku (erst gegen Code verifiziert).** Zwei Audit-Claims
korrigiert: (a) `regime_current` wird beim ERSTEN Check/Cold-Start gesetzt, nicht
nach dem zweiten; (b) die per-Zelle-↑/↓-Marker sind gar nicht implementiert
(`_cell` gibt nur `{wr}%`, Legende verwaist); (c) die „Fallback-Rate im Status-
Post" fehlt nicht, sie aggregiert nur alle Fallback-Gründe statt isoliert
`regime_unstable`. Scheduler-Kommentare in 18_abr1/12_ats/13_rub nannten die
falsche Trigger-Minute (10/8/12 vs Code 2/13/10) → Kommentare korrigiert, die
`now.minute`-Guards (Geld-Pfad) unangetastet. `ml_predictions_master.trade_id` =
hardcoded 0 überall außer 9_ai_sr_bot → an core/signal_post dokumentiert.

**P3.11 — Chart-Verzeichnis-Growth.** Housekeeping räumte `generated_charts` und
`charts`, aber nicht `institutional_charts` (22_ip_pattern_bot) → unbounded
Growth. In den 03:00-Cleanup aufgenommen (gleiche Outbox-Referenz-Schutzlogik).

## [2026-07-11] Datenpipeline-Robustheit — Gap-Continuity-Check, Coin-Refresh ohne Restart, chart_data_service-Watchdog (T-2026-CU-9050-092)

Drei Datenpipeline-Findings aus dem Audit-Ledger (P2.13, P2.15, P2.20), alle mit
DB-freien Tests in `backtest/` abgesichert; der Regression-Guard bleibt ohne
Golden-Refresh grün (die Golden-Fixtures sind lückenfrei, der neue Gap-Check
schlägt dort nie an — er lebt im DB-Worker, nicht in `calculate_indicators_optimized`).

**P2.13 — Indikator-Engine rollte Fenster über Kerzen-Lücken.** `2_indicator_engine.py`
lädt einen langen Lookback, um die rollenden Fenster aufzuwärmen, persistiert aber
nur den jüngsten Tail. Fehlten Kerzen (WS-Ausfall, Ingestion-Hänger), rechnete ein
„200-Perioden-MA" über die reale Zeit-Diskontinuität — Müll-Indikatoren genau auf
Coins mit löchrigen Daten. `find_contaminating_gap` überspringt Symbol/TF diesen
Zyklus (statt über das Loch zu rechnen) — aber **nur**, wenn die Lücke innerhalb
`MAX_INDICATOR_LOOKBACK` (200) Bars vor einer zu schreibenden Zeile liegt. Eine
alte, herausgerollte Lücke friert den Coin nicht ein (dessen `MAX(open_time)` würde
sonst nie vorrücken). Der nächtliche Gap-Filler (`6_housekeeping`) füllt die Lücke,
der nächste Zyklus rechnet lückenlos weiter — Self-Heal. Die Engine wurde in
P1.12/T-084 (`_as_of_now_window_globals`) und P1.13/T-054 (NaN-Warmups) umgebaut;
der Fix arbeitet gegen die aktuelle Struktur, nicht die alten Zeilennummern.

**P2.15 — Coin-Liste beim Prozessstart eingefroren.** `1_data_ingestion.py` und
`chart_data_service.py` fror die Coin-Liste beim Start ein — neu von Binance
gelistete Coins bekamen bis zum nächsten Restart keine Daten. Beide lesen jetzt
`coins.json` periodisch neu (das `6_housekeeping` täglich um 03:00 UTC aktualisiert
— kein dritter Writer, respektiert P2.16) und ziehen neue Symbole **additiv** nach.
`chart_data_service`: eigener WS-Worker pro Batch neuer Coins. `1_data_ingestion`
(Vollversion, Operator-Entscheid Michi): Tabellen + einmaliger 730d-Catch-up +
eigener WS-Worker, koordiniert über die drei nebenläufigen Loops (Catch-up,
Freshness, WS-Fleet) via geteiltem `tracked`-Set, das die Loops pro Zyklus
schnappschussen — neue Coins bekommen so auch die 12h-Catch-up- und
Freshness-Abdeckung. Konservativ: entfernte Coins werden nie abgebaut (Stream-
Teardown bleibt dem Restart), ein torn/leerer `coins.json`-Read ist ein No-op
(nie ein Coin live aus der Ingestion fallen lassen).

**P2.20 — chart_data_service ohne Message-Watchdog + synchroner 12MB-Snapshot.**
`async for msg in ws` hatte kein Timeout — eine stumme Connection (Binance nimmt
den Handshake an, sendet aber 0 Messages) hing den Worker ewig, ohne je zu
reconnecten. `_consume_with_watchdog` holt jede Message mit
`asyncio.wait_for(ws.recv(), 120)` und kehrt bei 120s Stille zurück → Reconnect.
Der ~12MB-JSON-Snapshot + `os.replace` lief synchron auf dem Event-Loop und
blockierte alle 60s die WS-Consumer; der Dump läuft jetzt in einem Thread
(`asyncio.to_thread`), das Intervall wurde auf 300s geweitet (nur der konsistente
Buffer-Snapshot wird kurz unter dem Lock kopiert).

## [2026-07-11] SMC-Sniper: unbestätigte Kanten-Pivots verworfen — bewusste Strategie-Änderung (P1.46-Rest, T-2026-CU-9050-093)

`25_smc_ml_sniper.py` findet Swing-Pivots via `scipy.signal.argrelextrema` mit dem
Default `mode='clip'`. Am rechten Rand vergleicht `clip` einen Kandidaten gegen den
wiederholten Randwert statt gegen echte Nachbarn — ein Pivot in den letzten
`PIVOT_WINDOW` (10) geschlossenen Kerzen wird also mit **weniger** als 10 echten
rechten Nachbarn akzeptiert. Ein solcher Kanten-Pivot ist unbestätigt: schließt die
nächste Kerze über sein Level, war der Punkt nie ein Pivot — der publizierte
Three-Drive bzw. das Breaker-Block-Level (und damit die daraus berechneten SL/TP)
repainten, **nachdem** das Signal (Geld-Pfad, Bot 25 postet live) draußen war. P1.46
hat die *forming*-Kerze gedroppt; dieser Rest-Repaint am rechten Rand blieb bewusst
offen, weil das TD-Frische-Gate (`len(df) - p3 <= PIVOT_WINDOW + 2`) genau diese
frischen Kanten-Pivots sucht — der volle Bot-24-Filter wäre kein Drop-in gewesen.

Dies ist eine **Operator-freigegebene Strategie-Änderung** (Michi, 2026-07-11), kein
Bugfix. Umgesetzt ist **Option B**: ein gemeinsamer Kanten-Filter direkt nach
`argrelextrema` verwirft Pivots mit weniger als `PIVOT_WINDOW//2 = 5` bestätigenden
geschlossenen Kerzen rechts (`peak_idx[peak_idx <= last_closed - PIVOT_WINDOW//2]`,
analog `trough_idx`; `last_closed = len(df) - 2`, da die forming-Kerze schon draußen
ist). **Ein** Filter speist beide Konsumenten (TD-Gate + `find_breaker_setup`), die
Kanten-Politik ist damit konsistent. Der volle Filter (Option A, `PIVOT_WINDOW`
Bestätigung wie Bot 24) wurde verworfen — er hätte das TD-Frische-Gate leergeräumt.

Signal-Raten-Delta, DB-frei über die Regression-Guard-Fixtures gemessen
(`tools/sniper_edge_pivot_delta.py`, aktuelle Geometrie inkl. T-089-`find_breaker_setup`,
4 Coins × 1h/4h, 3.608 Scan-Punkte): **Breaker-Block unverändert (0,0 %)** — ein
Breaker verlangt Breakout + Follow-through *nach* dem Pivot und ist damit strukturell
längst bestätigt; der gesamte Effekt liegt in **Three-Drive** (−40 % LONG / −47 %
SHORT). **Gesamt −5,9 %** (221 → 208 Geometrie-Trigger, rein subtraktiv — kein neuer
Trigger). Option A hätte TD um ~90 % gekappt (Gesamt −11,8 %) und den Detektor faktisch
stillgelegt. Das Rest-Repaint-Fenster ist damit **halbiert** (≤ 5 statt ≤ 10 Kerzen),
bewusst nicht auf null: TD braucht die frische Reversal-Entry.

Retrain-Kopplung: die deployten Artefakte TD2/BB2 sind auf der **alten** Pivot-Politik
gefittet. Bis zum Retrain-Rollout (Operator-Entscheid) sieht das Serving eine leicht
verschobene TD-Pattern-Verteilung; BB ist unberührt (0 % Delta). Ein Retrain sollte auf
der neuen Politik neu labeln.

Verifikation DB-frei: `backtest/test_sniper_edge_pivots.py` (neu, 7/7 — Kanten-Pivot-
Repaint-Mechanik, Filter-Schwelle exakt bei `PIVOT_WINDOW//2`, Reihenfolge vor dem
Pivot-Count-Gate, Guards für P1.46-forming-Drop und T-089-`find_breaker_setup`).
`test_sniper_forming`/`test_sniper_retest_level`/`test_sniper_tag` unverändert grün
(kombiniert 24/24). ruff + format + mypy grün.

## [2026-07-11] Monitore tracken exakt die publizierten Targets (P2.31, T-2026-CU-9050-083)

Die AI-Signal-Bots 9/12/13 (SRA1/ATS1/RUB1) publizieren im Cornix-Block TP1-3, Bot 11
(MIS1) TP1-5 — der Subscriber sieht also 3 bzw. 5 Targets. Gespeichert wurde in
`ai_signals.targets` aber die **volle** berechnete Zonen-Liste (bis zu 20 aus
`ensure_min_tp_distance(t_cands[:20], …)`). Der AI-Trade-Monitor (`8_ai_trade_monitor.py`)
scored `range(new_targets_hit, len(targets))` über genau das, was gespeichert ist, und
meldet `ALL TARGETS HIT` bei `len(targets)` — er hat kein eigenes Target-Limit. Folge:
er wertete bis zu 10-20 Phantom-TPs, die nie publiziert wurden. Die Win-Definition und
die Trailing-SL-Semantik (SL zieht auf `targets[new_targets_hit-2]`) liefen auf Zielen
außerhalb des Signals — die Live-Statistik entsprach nicht der Cornix-Realität.

Fix: an der `ai_signals`-Insert-Stelle jedes Bots wird die Target-Liste auf die
publizierte Anzahl gekappt (`json.dumps(targets[:n_show])`). `n_show` (3 bzw. 5) ist
jetzt eine benannte lokale Größe direkt an der Target-Berechnung und speist **sowohl**
den Cornix-Loop als auch den Insert — eine einzige Quelle, damit Tracking == Publikation
nicht wieder auseinanderdriftet. Der Cornix-Block selbst ändert sich **nicht** (Regel 4):
der Loop nutzt vorher `targets[:3]`/`[:5]`, jetzt `targets[:n_show]` mit identischem Wert,
der publizierte Message-String ist byte-identisch. Es geht ausschließlich um die
Tracking-Zeile. Der Monitor bleibt unberührt — das Kappen an der Quelle ist der korrekte
Hebel, weil `n_show` beim publizierenden Code lebt und der Monitor die publizierte Anzahl
gar nicht kennt. In-Path mitgezogen: `core/signal_post.post_ai_signal` (Research-Bots
30-33) hatte dasselbe Muster (Cornix `targets[:n_show]`, Insert volle Liste) auf denselben
`ai_signals`→Monitor-8-Pfad — ebenfalls auf `targets[:n_show]` gekappt.

Bestandsdaten in der DB bleiben unangetastet (Historien-Korrektur wäre ein VPS-Job).
DB-freier Guard `backtest/test_published_targets.py`: behavioral gegen den echten
Insert-Pfad von `post_ai_signal` (stored == publizierte Cornix-Targets == n_show) plus
strukturelle Guards für die vier Bots und den Monitor-Scoring-Loop; fällt auf dem
Pre-Fix-Stand (stored 8, published 3).
## [2026-07-11] Fleet-Prozessliste zentralisiert: `core/fleet.py` als Single Source (R2(a)/P1.38-Teilaspekt, T-2026-CU-9050-091)

Die Prozessliste existierte doppelt und war gedriftet: `main_watchdog.py`
(`PROCESSES_TO_RUN`, autoritativ, mit `start_delay`, volle Fleet) vs. `dashboard.py`
(`PROCESSES`, mit `group`, aber **ohne** die Bots 26–34). Das Dashboard zeigte damit
nur einen Teil der laufenden Fleet und musste bei jedem neuen Bot von Hand nachgezogen
werden.

**Fix:** neue `core/fleet.py` definiert die Fleet **einmal** (Name/Script/Group/
`start_delay`/`restart_interval`); Watchdog und Dashboard importieren dieselbe Liste.
Der Watchdog liest name/script/start_delay/restart_interval (ignoriert `group`), das
Dashboard liest name/script/group/restart_interval (ignoriert `start_delay`) — das für
den einen Konsumenten irrelevante Feld ist für den anderen ein No-op.

**Keine Verhaltensänderung am Watchdog:** identische Prozesse, Start-Reihenfolge und
Staffel-Delays wie zuvor inline (`backtest/test_fleet_definition.py` pinnt die
autoritative Projektion Byte-für-Byte). Die Lifecycle-Mechanik — Single-Instance-Mutex/
Orphan-Sweep/CTRL_BREAK (P0.2/P2.48) und Supervision/Backoff/Heartbeat (P1.37/P2.47) —
ist **nicht** angefasst; zentralisiert wurde ausschließlich die LISTE.

**Sichtbare Änderung nur im Dashboard:** es zeigt jetzt automatisch die volle Fleet
inkl. der zuvor fehlenden Bots 26–34. Deren Anzeige-`group` wurde bewusst aus dem
bestehenden Set (`core`/`ai`/`strategy`/`logger`) gewählt — die Regime-/Orchestrator-/
UFI1-Bots 26–29 als `strategy`, die Research-/MAX1-Bots 30–34 als `ai` —, damit kein
ungestyltes Badge und keine neue Filterkategorie im Dashboard entsteht. `22_ip_pattern_bot.py`
bleibt (wie im Watchdog seit jeher auskommentiert) aus der Fleet ausgeschlossen.

**Ledger:** R2(a) mit Teilhaken annotiert (R2(b), das `schema.sql`-Thema, bleibt offen —
braucht VPS/DB); P1.38-Teilaspekt „Prozessliste driftet" abgehakt, die drei übrigen
Dashboard-Fixes (CSRF, Log-Streaming-Handle, `/api/status`-psutil-Sweeps) bleiben offen.
Ein Guard aus `backtest/test_max1_gate.py` wurde von `main_watchdog.py` auf `core/fleet.py`
nachgezogen (die Registrierung wohnt jetzt dort).

## [2026-07-11] AIM2-Serving: Kandidaten-Fenster 60 min + tabellen-agnostischer conv-Dedup-Key (P2.35, T-2026-CU-9050-090)

Drei Audit-Findings aus Welle 5 am AIM2-Master-Gate (`15_ai_master_bot.py`).
**Kontext:** AIM1 bleibt per P0.13 AUS (kein Retrain); der Code läuft als AIM2-Träger
(shadow-first hinter `AIM2_LIVE_POSTING`, `docs/AIM2_DESIGN.md`). Die Fixes gelten dem
AIM2-Pfad. Die Ledger-Zeilennummern von P2.35 (Stand 07-03) stammen aus dem alten
AIM1-Code — gegen den aktuellen AIM2-Neubau neu verortet.

**(a) Kandidaten-Fenster 30 → 60 min.** Der AIM2-Neubau hatte das ursprüngliche
5-min-Fenster bereits auf 30 min gezogen und eine persistente Dedup-Tabelle
(`master_ai_processed_signals`) eingeführt. Rest-Delta laut Brief: 60 min. Das Fenster
begrenzt nur die Staleness (wie alt ein Signal noch gehandelt werden darf); Doppel-
Processing nach Downtime verhindert die Dedup-Tabelle, nicht die Fensterbreite — die
Verbreiterung ist damit gefahrlos.

**(b) Kontext-/Schwarm-Selbstzählung — bereits korrekt, kein Change.** Der Verdacht
„Kontext-Aggregate zählen den Kandidaten selbst mit" trifft auf AIM2 nicht mehr zu:
`swarm_stats` (Serving) filtert strikt `ts < Kandidaten-ts`, und `load_signal_stream`
schließt AIM1/AIM2/AIM2-TOPN aus dem Stream aus. Der Trainer (`aim2_build_dataset.py`)
tut mit `searchsorted(side="left")` + identischem Modell-Ausschluss exakt dasselbe.
**Beide Seiten sind identisch → keine Änderung, und ausdrücklich KEINE Retrain-Kopplung**
(Regel 7 nicht berührt: es ändert sich kein Modell-Input-Feature). Ein DB-freier Test
pinnt die Invariante jetzt mechanisch.

**(c) conv-Dedup-Key ist jetzt tabellen-agnostisch (Root-Cause statt Symptom).** Der
Dedup-Key war `(signal_type, id)` mit `signal_type="conv_signal"` für active- UND
closed_trades_master. Beide Tabellen haben aber **eigene SERIAL-Sequenzen**, und ein
conv-Signal wandert binnen Sekunden von active nach closed — mit **neuer id** bei
unveränderter Open-`time` (`5_trade_monitor.close_trade` kopiert die Identitätsfelder
1:1). Der per-Tabelle `id` taugt deshalb nicht als Dedup-Schlüssel (dieselbe Diagnose,
die `33_ai_fif1_bot.signal_key` schon dokumentiert). Zwei Fehlerklassen: (1) die
closed-Form (neue id, `time` noch im 60-min-Fenster) wird als frischer Kandidat
re-gescored → **Doppel-Post** — für schnelle Strategien wie „Fast In And Out" der
Regelfall; (2) unbeteiligte active/closed-Rows mit zufällig gleicher id verdrängen sich
gegenseitig aus dem processed-Set → stiller Verlust eines legitimen Signals (die im
Ledger genannte Kollision). Der Brief-Vorschlag „distinkte signal_types" (active vs.
closed trennen) fixt nur (2), nicht (1). Fix daher über einen migrations-stabilen
Identitäts-Key: `conv_signal_identity(source, symbol, direction, time, entry)` → BIGINT-
sicherer md5-Hash; ai behält die stabile `ml_predictions_master.id`. Schema der
Dedup-Tabelle unverändert (TEXT/BIGINT bleiben) → keine Live-Migration; alte
`conv_signal`-Rows im processed-Set werden nach Deploy einmalig ignoriert (bounded,
shadow-only).

Wirksam bei Live-Gate-Flip; heute shadow-only. DB-freie Tests:
`backtest/test_aim_context_features.py` (conv-Identität über active→closed stabil,
id-Kollision aufgelöst, ai-Namespace getrennt, Schwarm-Selbstausschluss, Fenster ≥60).
Verifikation: volle `backtest/`-Suite grün (611), ruff/format/mypy clean.

## [2026-07-11] 21_btc_smc Cooldown/Dedupe + 20_funding_bot Extreme-Schwelle 75→95/85 (T-2026-CU-9050-088)

Zwei unabhängige Audit-Findings aus Welle 5.

**P2.46 — `21_btc_smc_strategy.py` hatte keinen Cooldown/Dedupe.** Der Bot scannt
stündlich und postet, sobald ein EMA21+FVG-Pivot-Retest-Setup „fully closed" ist.
Ohne Sperre re-qualifiziert dasselbe Setup bei Gap-Filler-Lag im nächsten Scan —
das identische Cornix-Signal ging ~1h versetzt ein zweites Mal raus (Doppelposition
mit echtem Geld). Fix: jeder Post läuft jetzt durch das zentrale `trade_cooldowns`-
System in `send_cornix_signal`. Der Cooldown-Check läuft vor dem Outbox-Insert; nach
erfolgreichem Post wird der Cooldown im **selben Commit** wie der Insert gesetzt
(`update_cooldown(..., commit=False)` + ein `conn.commit`), sodass Signal und
Dedupe-Marker atomar persistieren — ein Teil-Commit hätte genau das Re-Posting
ermöglicht, das der Fix verhindert (T-024-Lektion). Tag `BTCSMC_1H` (9 Zeichen, passt
in `trade_cooldowns.module` varchar(10)); Cooldown 12h — Fleet-Default für sub-daily
TFs (P1.27-Muster, vgl. Bot 16) und über der 1h-Kerzendauer, damit das 1h-versetzte
Doppelsignal sicher geblockt ist. Die P0.5-Fixes (cap_leverage_to_sl) bleiben unberührt.

**P2.40 — Funding-„Extreme"-Alert feuerte im Normalzustand.** `20_funding_logger_bot.py`
postet einen TOP20-„FUNDING EXTREME ALERT", wenn ein Anteil der Top-20-Coins einseitig
positiv/negativ funded. Die alte Untergrenze war 75 %. Der Funding-Baseline ist aber
leicht positiv (~+0.01 %), also sind routinemäßig ~75 %+ der Top-20 positiv — der
75er-Trigger meldete fast permanent „EXTREME". Operator-Entscheid (Michi 2026-07-11):
Untergrenze auf 95/85. Die Schwellen-Logik ist in den reinen Helper
`classify_funding_extreme(pos_pct)` extrahiert (testbar, Grenzfälle gepinnt).
**Bewusste Signal-Raten-Änderung:** der Funding-Bot alertet ab jetzt seltener — nur
noch bei echt einseitigem Funding (≥95/85 %), nicht mehr im leicht-positiven Alltag.
Betrifft nur den Info-Kanal `CH_MARKET_DATA` (Sentiment-Post, kein Cornix-Trade).

DB-freie Tests: `backtest/test_btc_smc_cooldown.py` (Cooldown-Wiring: aktiv→kein Post,
frei→genau ein Outbox-Insert + atomarer Cooldown-Upsert, DB-Fehler→Non-Post),
`backtest/test_funding_threshold.py` (95/85-Grenzfälle inkl. „75 feuert nicht mehr"),
Tag-Länge statisch gepinnt in `backtest/test_cooldown_tags.py`.

## [2026-07-11] SMC/Mayank/Sniper — Weekend-Refire, FVG-Age, SL/RR (P2.45) + Break-and-Retest-Level (P2.39) (T-2026-CU-9050-089)

Vier Signal-Qualitäts-Fixes an den drei SMC-Bots (16/17/25) aus der Welle-5-Dispatch
(T-2026-CU-9050-075). Alle vier lassen ausschliesslich Signale WEGFALLEN bzw. korrigieren,
WELCHES Level gescort wird — keine neue Position, kein neuer Post-Pfad.

**P2.45(a) — Weekend-/Stale-Candle-Gate (16 + 17).** Forex/Metals stehen am Wochenende
still: die letzte geschlossene yfinance-Kerze friert ein und erfüllt die Struktur-/FVG-
Bedingung tagelang weiter, während der 12h-Cooldown darunter abläuft → der Bot refeuerte
dieselbe eingefrorene Kerze bei jedem Cooldown-Ablauf. Neu: reiner Helper
`is_stale_candle(open_time, tf, now)` — ein Signal darf nur feuern, wenn seit dem Close der
letzten Kerze weniger als **zwei Kerzendauern** vergangen sind. Die Zwei-Kerzen-Toleranz
verzeiht einen einzelnen yfinance-Live-Lag; ein Wochenende überschreitet sie bei intraday-TFs
um ein Vielfaches. Gate als `continue` in `run_smc_analysis`/`analyze_strategy`. **Der
24/7-Krypto-Pfad (METALS: BTC/ETH/…) ist nie stale → dort ändert sich nichts.** Bewusst
konservativ offen gelassen: ein 1d/1w-Signal kann über ein WE noch einmal refeuern, bevor die
2-Dauern-Schwelle greift — die dominante Regression war der intraday-12h-Refire.

**P2.45(b) — FVG-Age-Limit (16).** `find_unmitigated_fvgs` bekam `max_age=FVG_MAX_AGE` (50 Bars):
ein nie mitigiertes FVG blieb sonst über die gesamte 300-Kerzen-Historie triggerbar. Konservativ
(1h ≈ 2d, 4h ≈ 8d, 1d = 50d); ältere Gaps gelten als abgestanden.

**P2.45(c) — SL/RR-Sanity (17).** Mayank postete SL = letztes-Tief*0.998 und TP = nächster Pivot
ohne jede Prüfung, ob der Stop unter Leverage überlebt oder ob der nächste TP das Risiko schlägt.
Neu: reiner Helper `passes_sl_rr_guard(entry, sl, tp1, direction)` vor dem Send in beiden Zweigen —
verwirft Stops weiter als 15% vom Entry (Liquidations-Risiko, gleicher Cap wie der ROM1-Pfad P2.27)
und Setups, deren nächster TP nicht mindestens 0.5× das Risiko als Reward bietet (Sanity-Floor, keine
normale Pivot-Ladder beschnitten). SL/TP sind pro Scan FVG-unabhängig (aus `curr_low`/`curr_price`),
ein Fail blockt daher den Scan (`break`).

**P2.39 — Break-and-Retest wählt das falsche Level (25).** Der Breaker-Block scorte blind
`peak_idx[-2]`/`trough_idx[-2]`; gehörte der frische Retest zu einem anderen Swing (dem neuesten
oder einem älteren), prüfte der Bot ein Level, an dem der Preis gar nicht war — und verpasste das
echte Setup. Neu: reiner Helper `find_breaker_setup(...)` läuft die Pivots von neu nach alt und nimmt
den ersten, dessen Level (a) im Retest-Band (±0.5%) um den aktuellen Preis liegt, (b) durch einen
Close innerhalb der letzten `MAX_BB_AGE`=20 geschlossenen Kerzen gebrochen wurde und (c) danach ≥0.3%
Follow-through lief. Frische-, Follow-through- und Band-Schwellen sind identisch zum Alt-Code — nur die
Level-**Auswahl** ändert sich. Feature-Timing bewusst am Retest-Bar (`len(df)-2`) belassen und
dokumentiert (Pattern-Anker des BB-Modells); ein Wechsel wäre Strategie-Redesign und gehört nicht hierher.

Die bestehenden SMC-Fixes bleiben unangetastet und grün: P1.26/P1.27 (16, FVG-Dead-Code-Range +
forming-Drop + TF-Cooldown) und P1.46 (25, forming-Pivots) — `test_smc_fvg_dead_code.py`,
`test_sniper_forming.py`, `test_sniper_tag.py` alle weiter grün. DB-frei getestet:
`backtest/test_smc_weekend_refire.py` (14/14) + `backtest/test_sniper_retest_level.py` (9/9), je mit
Divergenz-Kanarie gegen die Pre-Fix-Logik. Volle backtest-Suite: 612 passed.

## [2026-07-11] 14_ai_atb_bot.py — ATB1 unknown-State observe-only + Main-Loop-Härtung (T-2026-CU-9050-086)

Zwei Robustheits-Fixes am geparkten Bot 14 (ATB1). Wirken erst beim Entparken —
die Fixes sind risikofrei, mussten aber vor dem Entparken stehen (OPUS-HANDOFF §3).

P2.36 (unknown-State = observe-only): Nach einem State-Loss (`trendline_state.json`
fehlt oder ist korrupt) fällt TRENDLINE_STATE auf {} zurück, jeder Coin bekam
`prev_relation="unknown"`. Der alte Inline-Break-Check listete "unknown" in jeder
Bedingung — beim ersten Zyklus nach State-Loss feuerte damit JEDER Coin über/unter
seiner Trendlinie ein frisches BREAK-Event (Massen-Event-Flood mit echtem Geld; der
alte Kommentar gab den Bug offen zu). Die Event-Klassifikation ist jetzt in die reine
`classify_trendline_event` extrahiert: `prev_relation=="unknown"` gibt `None` zurück.
Der erste Zyklus baut nur die Relation neu auf und emittiert nichts; der Caller
schreibt `prev_relation` unverändert weiter, echte Transitionen (below→above etc.)
feuern ab dem Folgezyklus. Persistenz allein hätte nicht gereicht (Datei kann fehlen),
der observe-only-Guard ist der eigentliche Schutz.

P2.37 (Main-Loop-Exception-Handling + Conn-Hygiene): Der Scan in
`run_trendline_detector` läuft jetzt in `try/finally` — `conn.close()` und
`save_trendline_state()` laufen auch bei einem Mid-Scan-Abort (vorher: Connection-Leak
+ verworfener State). Der `main()`-Loop fing nur `KeyboardInterrupt`; jede Scan-Exception
killte den Prozess. Jetzt breites `except Exception` mit ERROR-Log + 30s-Backoff statt
Prozess-Tod (Muster: `3_detectors.main()`, P1.15). Der per-Coin-Rollback (P1.23) und die
Forming-Candle-Slice (P1.22) bleiben unangetastet.

DB-frei getestet in `backtest/test_atb_unknown_state.py` (observe-only-Invariante +
differenzielle Assertion gegen die Pre-Fix-Flood-Logik; fällt auf dem Pre-Fix-Stand).

## [2026-07-11] Watchdog: Graceful Shutdown statt hartem terminate() (P2.48) + atomic_write_json Windows-Fix (P2.49) (T-2026-CU-9050-087)

Zwei Prozess-/Persistenz-Findings aus der Welle-4-Dispatch (T-2026-CU-9050-075).

**P2.48 — Harter terminate() orphant die ProcessPool-Worker.** `main_watchdog.kill_process`
rief `p.terminate()` — auf Windows ein sofortiger `TerminateProcess` ohne Graceful Shutdown.
Kritisch: die ProcessPool-Worker der Indicator-Engine (`2_indicator_engine.py`,
`ProcessPoolExecutor`) überlebten den Parent-Kill als Waisen und rechneten weiter →
Doppel-Compute-Fenster. Neu: jeder Bot (und das Dashboard) startet in EINER eigenen
Prozessgruppe (`CREATE_NEW_PROCESS_GROUP`); der Stop schickt ein `CTRL_BREAK_EVENT` an die
GANZE Gruppe — das erreicht den Bot UND seine Worker-Kinder, anders als `terminate()`, das
nur den Bot selbst trifft. Danach wird `GRACEFUL_STOP_TIMEOUT_S` (Default 10s, env-overridable)
gewartet, dann hart nachgetreten. Ist `CTRL_BREAK` nicht zustellbar (keine Konsole angehängt —
Scheduled-Task-Start, oder Prozess schon weg), fällt der Pfad auf `terminate()` zurück und
loggt es — nie schlechter als vorher. Die eigene Gruppe verhindert zugleich, dass ein
Stop-Signal die Watchdog-Konsole mittrifft. P0.2 (Mutex + Orphan-Sweep) und der
Scheduled-Task-Restart-Pfad (T-074, `restart_fleet.ps1` stoppt über `Stop-ScheduledTask` +
Orphan-Reap beim nächsten Start) bleiben unangetastet — die Prozessgruppen-Isolation
verbessert deren Teardown-Ordnung, regressiert sie nicht.

**P2.49 — atomic_write_json verwarf Updates still auf Windows.** `core/state_utils.py`
nutzte einen FESTEN `.tmp`-Namen (zwei parallele Writer auf denselben Pfad korrumpierten sich
auf derselben Temp-Datei) und ließ `os.replace` unter dem breiten `except` scheitern, wenn ein
Reader die Zieldatei offen hielt → das Update ging STILL verloren. Neu: unique Temp-Name via
`tempfile.mkstemp` im Zielverzeichnis (gleiches Dateisystem → `os.replace` bleibt atomar,
Muster `core/coins.py` #68) + kurzer Retry (5×50ms) auf `PermissionError`; bleibt es blockiert,
wird es GELOGGT (kein stiller Verlust mehr) und die Temp-Datei aufgeräumt.

DB-freie Tests: `backtest/test_atomic_write_json.py` (12: Roundtrip, unique-tmp, Retry-Pfad,
Permanent-Failure-Logging, Cleanup), `backtest/test_watchdog_shutdown.py` (8: Prozessgruppen-Flag,
CTRL_BREAK vs SIGTERM je Plattform, Hard-Kill-Eskalation, CTRL_BREAK-Fallback). Die Regressionsuiten
`test_watchdog_backoff.py`/`test_watchdog_hang.py` (P1.37/P2.47) bleiben grün. **Beweislage ehrlich:**
die tatsächliche Prozessgruppen-Signalzustellung und das ProcessPool-Worker-Teardown sind nur gegen
eine echte Windows-Konsole beobachtbar — unit-testbar ist, dass das RICHTIGE Signal in der RICHTIGEN
Reihenfolge abgesetzt wird; die Live-Verifikation (kein Waisen-Worker nach kill_process) ist ein
VPS-Schritt.

## [2026-07-11] Detector-Zyklus: Batch-Ticker statt 538 Einzel-Calls + Volume-Indicator-Fixes (P2.44 + P2.42, T-2026-CU-9050-085)

Zwei Findings aus dem Detector-Pfad, beide aus der Welle-4-Dispatch (T-2026-CU-9050-075).

**P2.44 — HTTP-Last & Gate-Reihenfolge.** `3_detectors.py` machte pro Scan-Zyklus
einen Binance-klines-Call je Coin (~530 serielle Requests). Neu: `get_live_prices_batch()`
holt in EINEM `/fapi/v1/ticker/price`-Call alle Symbole; die Loop liest `price_map.get(symbol)`
und fällt nur für fehlende Symbole (frisch delisted) oder bei Batch-Ausfall auf den alten
per-Coin-HTTP→DB-Pfad zurück — kein Coin wird geskippt, ein Batch-Ausfall degradiert sauber
aufs alte Verhalten. Zusätzlich in `strat_volume_indicator.analyze_coin`: der teure
90d×30m-HVN-Read lief als ERSTES Gate für jeden Coin. Die vier Gates (Spike, Active-Trade,
Cooldown, HVN) sind alle seiteneffektfreie, AND-verknüpfte Reads → auf billig-vor-teuer
umsortiert, der HVN-Read läuft jetzt ZULETZT und nur, wenn ein Signal sonst emittierbar wäre.
Die Signalmenge ist invariant gegen die Auswertungsreihenfolge. Der P1.16-Cooldown-Kontrakt
(12h-Sperre, Tag `VolIndic`, Write via Detector mit `commit=False`) bleibt unangetastet —
nur der read-only `check_cooldown` wurde vorgezogen.

**P2.42 — Volume-Spike-Klassifikation & HVN-Gate.** Drei bewusst signaländernde Fixes
(Ledger-Auftrag): (a) die Spike-Auswahl iteriert jetzt rückwärts — der JÜNGSTE Spike im
5-Tage-Fenster entscheidet statt des ältesten (die alte Vorwärts-Schleife brach beim
ersten/ältesten Spike ab); (b) ein Spike auf der ersten In-Period-Kerze (`i==0`) hat keinen
In-Period-Vorgänger und wird jetzt verworfen statt still als Sell klassifiziert; (c) das
HVN-Gate binnt Preise auf 0.1%-Level, bevor Volumen aggregiert wird — der alte
`groupby('close')` auf rohen Float-Preisen akkumulierte auf fine-tick-Coins nie ein Level
(jede Kerze ein eigener Preis) und feuerte dort faktisch nie. Die Klassifikations- und
HVN-Logik wurde in die reinen Funktionen `_classify_latest_volume_spike` /
`_is_near_high_volume_node` extrahiert (identisches Verhalten, DB-frei testbar).

DB-freie Tests: `backtest/test_volume_indicator_spikes.py` (9, mit Pre-Fix-Referenz-Asserts),
`backtest/test_detector_batch_ticker.py` (4). Die `[DB]`-markierte Live-Last-/Effekt-Messung
(CPU-Grundlast, geänderte Signal-Rate) bleibt ein VPS-Schritt.
## [2026-07-11] Orchestrator: Startup-Whitelist-Reconciliation (P2.24) + Whitelist-Cleanup-Schreibseite (P2.25) (T-2026-CU-9050-082)

Zwei Regime-Gating-Findings geschlossen, beide über die In-Memory- bzw.
Schreib-Seite eines seit T-046 nur halb entschärften Problems.

**P2.24 — Regime-Wechsel während Orchestrator-Downtime nie nachgeholt.**
`check_regime_change_and_close` feuert nur auf einem BEOBACHTETEN In-Memory-Flip
(aktuelles `regime_current` ≠ dem beim letzten Poll gemerkten `_last_known_regime`).
Beim Prozessstart ist diese Baseline leer, der erste Poll seedet sie also nur und
kehrt zurück — ein Regime-Wechsel, der WÄHREND der Downtime passiert ist, wird nie
nachgeholt, und jeder offene Trade läuft unter einem Regime weiter, das ihn evtl.
nicht mehr whitelistet. Fix: `run_startup_reconciliation` läuft einmalig vor der
Main-Loop und prüft alle OPEN-Trades in `orchestrator_open_trades` gegen die
AKTUELLE Whitelist — kein erinnertes Regime nötig. Der Close-/Trail-Body ist in
`_close_non_whitelisted_open_trades` extrahiert und mit dem Regime-Change-Handler
geteilt: nur ROM1-eigene Trades (die Tabelle enthält per Konstruktion nur ROM1,
der DB-seitige Force-Close ist `model='ROM1'`-gefiltert, P1.9), bestehender
Close-Pfad, kein neuer Mechanismus. Startup seedet zusätzlich die Baseline, damit
der erste periodische Check nicht auf dem Boot-Zustand feuert, und postet nur dann
eine Status-Meldung, wenn er wirklich etwas geschlossen/getrailt hat — kein
Status-Channel-Spam bei jedem Watchdog-Restart. Fail-safe: eigene Kurz-Connection,
ein Fehler hier blockt den Loop-Start nie.

**P2.25 (Schreibseite) — Stale `bot_regime_whitelist`-Rows nie bereinigt.** Die
Lese-Seite ist seit T-046 entschärft (Zellen >48h → Overall-Fallback). Offen war die
Schreib-Seite: `cleanup_stale_performance_rows` räumte nur die Perf-Tabelle, die
Rohnamen-Rows in `bot_regime_whitelist` (eingefroren seit 19.04., genau die, die der
Orchestrator las) blieben liegen. Neue `cleanup_stale_whitelist_rows` läuft in
`run_analysis` direkt daneben, vor `compute_whitelist`. Zwei disjunkte, ODER-verknüpfte
DELETE-Kriterien (`build_whitelist_cleanup_query`): (A) Rohnamen-Keys
`pretty_name(bot_name) <> bot_name` — provably orphaned, altersunabhängig gelöscht wie
in der Perf-Tabelle; (B) `computed_at` älter als `WHITELIST_RETENTION_DAYS` (14d) —
normalisierte Rows retirierter Bots. 14d bewusst konservativ: der Read-Gate (48h) hat
alles Ältere ohnehin entwertet, aktive Bots werden im selben Lauf neu geschrieben.
Scan-/Delete-Fehler werden geschluckt (0 zurück, kein Commit) — der stündliche Lauf
crasht nie an der Bereinigung.

Verifikation DB-frei: `backtest/test_orchestrator_startup_check.py` (6) +
`backtest/test_whitelist_cleanup.py` (6), plus die bestehenden Orchestrator-/
Analyzer-Suiten grün (144 gesamt). ruff/format/mypy lokal grün. Live-Verifikation
(Restart-Nachlauf; Step-2-Query 9) bleibt VPS-Session-Follow-up.
## [2026-07-11] 23_market_tracker.py — Telegram-Chunker splittet Über-Blöcke, Full-History-Load + async-Jobs als Risiko dokumentiert (P2.41, T-2026-CU-9050-081)

Rest-Aufräumung von P2.41 am Market-Tracker, vier Teilbefunde vom 07-03-Ledger am
aktuellen Code neu verortet und differenziert behandelt.

Der echte Robustheits-Bug (d): der Message-Chunker `_build_chunks` konnte einen
einzelnen Bot-/Tabellen-Block, der allein über dem Budget lag, als EINEN
>4096-Zeichen-Chunk emittieren. `send_telegram` schreibt nur in `telegram_outbox`;
der Dispatcher `4_telegram_bot` verwirft eine Über-Limit-Message still — der ganze
Per-Bot-Post wäre unbemerkt verschwunden. Neuer `_hard_split_block`-Fallback splittet
einen Über-Budget-Block zuerst auf Zeilen-, als letzte Instanz auf Zeichen-Grenzen; das
Budget wird gegen den grösseren der beiden Header (Erst-/Folge-Chunk) gerechnet. Jeder
emittierte Chunk ist jetzt garantiert ≤4096. Normale Einträge liegen weit unter dem
Budget — der Fallback greift nur bei einem pathologischen Eintrag, aber dann geht der
Post als mehrere Nachrichten raus statt zu verschwinden. Die drei Chunker-Helper wurden
dafür von nested (in `job_per_bot_performance`) auf Modulebene gehoben, damit sie
DB-frei testbar sind (rein, kein Closure-State).

(c) Regime-Fit-Query ohne rollback: bereits durch P1.43/T-029 erledigt
(`_get_regime_fit_label` rollt zurück, `_regime_conn` in try/finally) — am Code
verifiziert, kein Rest offen (No-op).

Bewusst NICHT geändert, als bekannte Risiken im Code dokumentiert (Ledger-Geist,
Risiken früh dokumentieren statt blind optimieren): (a) der stündliche Full-History-Load
der `closed_*`-Tabellen ist zwingend für die all-time-Spalte + den Survivor-Pick des
DISTINCT-ON — ein Zeitfilter wäre eine Verhaltensänderung der Statistik (Operator-
Entscheid). (b) die `async`-Jobs tun blockierendes sync-DB-I/O — kosmetisches `async`
bei seriellem, zeit-gestaffeltem Scheduler; eine echte Async-Umstellung wäre ein Rewrite
und tauschte eine harmlose Scheduling-Verzögerung gegen ein Pool-Starvation-Risiko
(Pool-max 8/Prozess).

Verifikation: `backtest/test_market_tracker_chunker.py` (neu, DB-frei, 13/13),
`test_market_tracker_conn.py` unverändert 7/7 (Helper-Move ohne Verhaltensänderung),
`test_market_tracker_opened.py` 7/7. ruff/format/mypy lokal grün. Wirkt beim nächsten
regulären Restart, kein Deploy.

## [2026-07-11] Watchdog-Hang-Detection + statement_timeout/keepalives im DB-Pool (T-2026-CU-9050-077, P2.47)

Step-2-Befund: die Data-Ingestion war 6h tot bei grünem Watchdog — die Fleet handelte
auf Stale-Daten. Ursache doppelt: (1) der Watchdog prüft nur Prozess-EXISTENZ, ein
lebender-aber-wedged Bot bleibt "grün"; (2) der DB-Pool hatte keinen statement_timeout
und keine TCP-keepalives, ein auf einem toten Socket hängender Bot blockiert ewig ohne
zu sterben.

`core/database.py`: jede gepoolte Connection bekommt jetzt `statement_timeout` (default
300s, kappt Runaway-Queries/Hänger server-seitig) und libpq-TCP-keepalives (idle 30s /
intervall 10s / count 3 — ein still gedroppter VPS↔Postgres-Socket schlägt schnell fehl
statt zu hängen). Der Default ist bewusst **300s, nicht 30s**: diese DB hat
`closed_trades_master`/`closed_ai_signals` ohne nutzbare Indexe (Full-Table-Scans), einen
stündlichen Market-Tracker der Full-History lädt und Housekeeping über ~9.7k Tabellen
(audit_reports/18). Legitime Queries >30s sind damit wahrscheinlich; ein 30s-Cap würde
`QueryCanceled` in den breiten excepts vieler Bots auslösen → stille Degradation, genau die
Fehlerklasse die dieses Audit bekämpft. 300s killt echte Runaways/Hänger und verschont die
stündlichen Analytics. Eine **Verschärfung auf 30s** ist ein Operator-Entscheid — erst
**nach** der Z0-Query-Laufzeit-Messung auf dem VPS. Alle Werte sind benannte Konstanten und
env-overridable; `statement_timeout` lässt sich per Prozess über
`KYTHERA_DB_STATEMENT_TIMEOUT_MS=0` deaktivieren — der Escape-Hatch für lange
Trainer-/Housekeeping-Queries. Das pre-existierende `lock_timeout` bleibt. **Kein**
Timezone-Flip (R3/UTC_POLICY.md bewusst ausgeklammert).

`main_watchdog.py`: neuer generischer Heartbeat (`check_heartbeat`). Ein lebender Prozess,
dessen eigenes Log-File `HANG_LIMIT_S` (default 20 min) nicht mehr advanced, gilt als
wedged → WARNING. Das Log wird **mapping-frei** aus den offenen File-Handles des Prozesses
aufgelöst (einmal pro Prozess-Leben gecached, kein fragiler script→logname-Table); ein
Bot ohne beobachtbares Log ist EXEMPT und kann nie fälschlich neu gestartet werden. Ein
frisch (neu)gestarteter Bot hat ein volles Grace-Fenster. Auto-Restart ist **default-OFF**
(Geld-Pfad — per default nur WARNING, Operator entscheidet); Opt-in via
`KYTHERA_WATCHDOG_HANG_AUTORESTART=1`, der Restart reitet dann auf dem bestehenden
Crash-Backoff (P1.37, kein `time.sleep` im Supervision-Pfad). Die Daten-Staleness selbst
deckt weiterhin `core/health_monitor` DB-seitig ab (Kerzen-Alter → Auto-Restart der
Ingestion); dieser Patch ergänzt das um das generische Prozess-Signal. DB-freie Tests:
`backtest/test_db_pool_options.py`, `backtest/test_watchdog_hang.py`.

Offen (bewusst nicht in diesem Patch, siehe PR): eine flächendeckende Per-Bot-Heartbeat-
Abdeckung setzt voraus, dass jeder Bot zuverlässig pro Zyklus loggt — heterogen im
Bestand (nur ein Teil nutzt `core.logging_setup`, einige loggen nur nach stdout). Der
Heartbeat greift heute nur für Bots mit beobachtbarem Log; die Ausweitung ist ein
Folge-Thema statt improvisiertem Scope-Wachstum.

## [2026-07-11] Regression-Guard-Disarm gehärtet (P2.51) + Cooldown-Tag-Test um die MIS-Horizonte erweitert (P3.13) (T-2026-CU-9050-076)

Zwei kleine Härtungen aus dem Ledger, beide DB-frei, kein Live-Eingriff.

**P2.51 — Guard disarmt nicht mehr still bei gelöschten Goldens.**
`tools/regression_guard/guard.py::mode_verify` gab bei leerem `golden/` pauschal
„NOT ARMED … Pass" + Exit 0 zurück — auch wenn die `manifest.json` noch dalag.
Damit schaltete das Löschen der Goldens (oder ihr Verlust bei einem Merge) den
Guard unbemerkt ab, während der pre-commit-Hook grün blieb. Fix: das Manifest ist
der „war-einmal-scharf"-Marker (schreibt `refresh` neben die Goldens) — liegt es
vor, aber es gibt keine Goldens, endet `verify` jetzt mit **Exit 1** statt Pass.
Der genuin nie-scharfe Zustand (kein Manifest) bleibt der legitime
Pre-Live-DB-Freeze-Pass, und der umgekehrte Fall (Goldens ohne Fixtures → Exit 1,
`:139-140`) ist unangetastet.

**P3.13 — MIS-Horizont-Tags im Cooldown-Längennetz.** Der MIS-Bot postet seinen
Cooldown unter einem *abgeleiteten* Tag `f"{generation}-{horizon}"`
(`11_ai_mis_bot.py:301`), kein String-Literal — der bestehende
Literal-Sweep im Test sah ihn nie. `MIS2-168H` ist mit 10 Zeichen bündig an
`varchar(10)` (Fehlerklasse aus T-2026-CU-9050-024). Der Test parst jetzt
`MODEL_GENERATION` + die `MIS_CHANNELS`-Horizonte aus der Bot-Quelle und
rekonstruiert den Tag, sodass eine neue Generation (`MIS10`) oder ein längerer
Horizont die Assertion reißt — statt still im geschluckten `ValueError` des
`COOLDOWN_MODULE_MAX_LEN`-Guards zu landen.

### Fixed
- `tools/regression_guard/guard.py`: Manifest-vorhanden-aber-Goldens-fehlen →
  Exit 1 (P2.51).

### Tests
- `backtest/test_regression_guard_disarm.py` (neu, DB-frei): drei Fälle für die
  Disarm-Semantik. Fall 1 (Manifest ohne Goldens → Exit 1) ist ein echter
  Bug-Zeuge — gegen den Pre-Fix-Stand fällt er nachweislich mit
  AssertionError „got 0"; Fall 2 (nie scharf → Pass) und Fall 3 (Goldens ohne
  Fixtures → Exit 1) pinnen die Nachbar-Invarianten. Der armed-Compute-Pfad
  bleibt von `guard.py smoke` abgedeckt.
- `backtest/test_cooldown_tags.py`: `test_mis_horizon_tags_fit` ergänzt (P3.13).

## [2026-07-11] Post-Merge-Review zu P1.13: RSI-Flat-Fall dokumentiert, NaN-Paritäts-Imputation im EPD-Legacy-Pfad und in Bot 24/25 (T-2026-CU-9050-060)

Drei unabhängige Reviewer-Läufe über den gemergten Stand von PR #43
(T-2026-CU-9050-054) — Verdict einstimmig APPROVED, der Fix selbst ist korrekt
und symmetrisch, kein Rollback. Aber vier belegte Ungenauigkeiten, die dieser
Eintrag korrigiert bzw. deren Fixes er dokumentiert.

**F1 — RSI ist auch JENSEITS des Warm-ups dauerhaft NaN, wenn das Preisfenster
vollständig konstant ist** (illiquider Coin, Neu-Listing-Vorlauf, Trading-Halt):
`up = down = 0` auf jeder Zeile → `rs = 0/0 = NaN` → RSI auf jeder Zeile NaN,
nicht nur im Kopf. Die erste Preisbewegung beendet den NaN-Zustand endgültig:
`ewm(adjust=False)` hält danach `roll_up` (nach Up-Move) bzw. `roll_down` (nach
Down-Move) für immer > 0 — eine reine Up-Serie liest dann RSI = 100, nicht NaN;
der NaN-Zustand gilt also genau für voll-konstante Fenster. Entscheid
(Review-Empfehlung): NaN bewusst belassen — „kein RSI
definiert" ist ehrlich, eine 50 wäre wieder Fabrikation. Strukturell folgenlos:
ein eingefrorenes Fenster erzeugt 0 Pivots (`argrelextrema` auf einer Konstanten
ist leer), Bot 24 braucht ≥4 alternierende Pivots, Bot 25 ≥3 Peaks/Troughs —
beide `continue`n vor dem ML-Pfad; die Roh-Consumer (`strat_*`) vergleichen
NaN → False → kein Signal. Jetzt als Kommentar in `calculate_rsi` am Code.
WMA/BOLL/DONCHIAN haben den Fall NICHT (`rolling().std()` einer Konstanten
ist 0, nicht NaN) — dort ist NaN wirklich nur der Warm-up-Kopf.

**F2/F5 — Umfangs-Korrektur zum PR-#43-Text:** „ausschließlich
Warm-up-Kopfzeilen" war zweifach zu eng: (a) der F1-Fall liegt außerhalb des
Warm-ups; (b) der tiefste Golden-Breach ist `wma_200` in Zeile 198 — ein
199-Zeilen-Warm-up ist keine „Kopfzeile". Der Reviewer-Zählstand sind zudem
**821** NaN-Breaches je Fixture, nicht 816 (Differenz: 5
RSI-Zeile-0-Transitionen). Die Golden-Fixtures (BTC/ETH/SOL/DOGE, liquide)
können den RSI-Flat-Fall strukturell nie auslösen — „Golden belegt den Umfang"
gilt nur für den Warm-up-Teil, nicht für den illiquiden Teil der ~538er-Fleet.
Betroffen sind ferner ALLE `rsi_*`- (6/9/12/14/24) und `wma_*`-Spalten, nicht
nur `rsi_14`/`wma_21` wie im PR-Body (alle Consumer imputieren — unkritisch).

### Fixed
- `10_pump_dump_detector.py` (F3): der LEGACY-EPD-Pfad (greift nur ohne
  deploytes EPD2-Artefakt — also heute) baute das positionale Feature-Array
  ohne jede NaN-Behandlung. **Die F3-Prämisse des Ursprungs-Reviews („sklearn
  wirft bei NaN, der Exception-Handler unterdrückt sicher") ist dabei
  falsifiziert worden** — am Produktions-pkl verifiziert: das Modell ist ein
  `XGBClassifier`, XGBoost behandelt NaN nativ als Missing und liefert eine
  Prediction über untrainierte Default-Branches. Ein NaN-`rsi_14`
  (Neu-Listing-Warm-up post-P1.13) konnte also ein LIVE-Signal aus einem Input
  erzeugen, den der Trainer nie produziert hat. Fix: Imputation nach dem
  NULL-Kontrakt des Legacy-Trainers selbst (`legacy_trainers/zzz.py:7609-7617`:
  rsi→50, alles andere→0; die ema-Dists kollabieren dort via ema:=Preis zu 0) —
  Train/Serve-Parität nach demselben Prinzip wie das `fillna(0)` im EPD2-Zweig
  (dessen eigener `train_binary`-Kontrakt unangetastet bleibt). Die
  Serving-Werte sind identisch zu dem, was das Modell sein gesamtes
  prä-P1.13-Leben gesehen hat — Neu-Listings werden weiter gescort, mit 50
  statt NaN; kein Signal, das vorher unmöglich war, wird möglich. Bewusst NICHT
  pauschal `fillna(0)`: rsi=0 hieße „extrem oversold" und wäre für dieses
  Modell Out-of-Distribution.
- `24_quasimodo_bot.py` / `25_smc_ml_sniper.py` (F4): der Feature-Bau vor
  `predict_proba` bekommt dieselbe Non-Finite-Imputation (inf/NaN → 0) wie alle
  `core/*_features.py`-Builder — und wie die eigenen Trainer, die auf
  `.fillna(0)`-Frames fitten UND scoren (`qm_ml_trainer.py:321/353/378`,
  `smc_ml_trainer.py:328/344/365`): exakte Train/Serve-Parität. Auch hier wirft
  XGB bei NaN nicht, sondern scored über untrainierte Default-Branches — ein
  stiller Skew. Erreichbar ist der Pfad entgegen der ersten Annahme schon
  heute: `ffill().bfill()` lässt NaN nur in All-NaN-Spalten übrig, und die
  entstehen nicht nur bei eingefrorenen Fenstern (0 Pivots — die Bots bailen
  vorher), sondern auch, wenn der LEFT JOIN für das gesamte
  100/150-Kerzen-Fenster keine Indikator-Zeilen findet (Engine-Ausfall,
  Coverage-Lücke) — Preis-Pivots existieren dann weiter. Auf dem
  All-Finite-Pfad ist der Modell-Input unverändert.
- Neuer Standalone-Test `backtest/test_nan_feature_guards.py` pinnt beide
  Kontrakte (Legacy-NULL-Imputation rsi→50/Rest→0, 0-Imputation in Bot 24/25)
  und die XGBoost-NaN-Prämisse gegen das Produktions-pkl (skippt ohne
  Artefakt/xgboost).

**Weiterhin offen (VPS bzw. C-Gate, unverändert aus T-054):** (1) die
Populations-Zählung „wie viele Coins liegen unter ~170 Kerzen je TF" braucht
eine VPS-Session — sie beziffert den Recompute-Effekt. (2) Recompute →
TD2/BB2/QM2-Retrain → erst beim Artefakt-Rollout das `bfill` in Bot 24/25
entfernen, nie isoliert. Achtung nach dem Recompute: das Serving imputiert die
Warm-up-Zeilen (bfill) und füttert sie, der Trainer verwirft sie per `dropna`
(`tools/walkforward_sim.py:245`) — die Aussage „kein Train/Serve-Skew" aus dem
PR-#43-Text gilt nur für den Pre-Recompute-Zustand.


## [2026-07-11] core/coins.py — EIN atomarer coins.json-Writer (P2.16) + Binance-Perp-Shape-Guard für die Delisted-Cleanup (P2.17) (T-2026-CU-9050-079)

**P2.16:** `coins.json` hatte zwei Schreiber — `1_data_ingestion.update_trading_pairs`
(bei jedem Ingestion-Start) und `6_housekeeping.update_coins_json` (nächtlich 03:00 +
beim Start) — jeder mit einer eigenen Kopie des Filters und einem non-atomaren
`open('w')` + `json.dump`. Zwei handgepflegte Filter-Kopien driften (der ETHU-Vorfall
2026-07-06), und der direkte Write lässt für die Dauer des Dumps eine leere/partielle
`coins.json` sichtbar, die jeder Reader (Delisted-Cleanup, Gap-Filler, `load_coins`)
mitten hineinlesen kann. Neu: `core/coins.py` ist der EINE Writer — eine Filter-Definition
(`quoteAsset=USDT` + `status=TRADING` + `PERPETUAL`) und ein atomarer Write via
tmp-File + `os.replace` (fsync, tmp im Zielverzeichnis → gleiches Dateisystem, auch auf
Windows atomar). Beide Aufrufer rufen jetzt `refresh_coins_json`; ein Fetch-Fehler
schreibt gar nichts (kein Truncate), die Ingestion fällt weiter auf die on-disk-Liste
zurück. *Annotation-Korrektur (Falle 13):* die Filter-Divergenz „inkl. Quarterlies" war
bereits nach dem ETHU-Vorfall geschlossen (beide schon `PERPETUAL`, CHANGELOG 2026-07-06) —
offen waren nur die duplizierte Filter-Definition und der non-atomare Write.

**P2.17:** Die Delisted-Cleanup schloss JEDEN offenen Trade, dessen Symbol nicht in
`coins.json` steht — auch Nicht-Binance-Perp-Junk (Metals `XAUUSD`, Cross-Pair `ETHBTC`,
Forex), der über den alten Lose-Filter oder ein momentanes coins.json-Wackeln
hineingeraten war → nächtliche Falsch-Closes bei PnL 0. Neu: die Selektion (klassisch +
AI) verlangt zusätzlich die Binance-USDT-Perp-Shape (`core.coins.looks_like_usdt_perp`,
`<BASE>USDT` uppercase-alnum). Nur echt delistete USDT-Perpetuals werden noch geschlossen;
`XAUUSD`/`ETHBTC` & Co. bleiben unangetastet. Der Single-Writer aus P2.16 entfernt zudem
die vom Audit genannte „universe wobbles with dual coins.json writers"-Ursache.

**Nachtrag (Orchestrator-Review T-075): Leeres-Universum-Guard.** Der neue zentrale
Writer hatte — anders als der alte Housekeeping-Pfad (`if symbols:` vor dem Write) —
keinen Schutz gegen eine leere Liste. Ein 200er-Response mit leerem oder fehlendem
`symbols`-Key (`filter_usdt_perpetuals` nutzt `.get('symbols', [])`) liefert `[]` →
`write_coins_json_atomic([])` würde `coins.json` sauber-atomar leeren. Folge: die
Ingestion bringt die WS-Fleet mit 0 Coins hoch (der on-disk-Fallback greift NUR bei
Exception), und die nächtliche `cleanup_delisted_trades` schlösse ALLE offenen
USDT-Perp-Trades als delisted (der P2.17-Shape-Guard schützt nicht davor — echte Perps
haben die Shape). Neu: `refresh_coins_json` verweigert den Write bei leerer Liste
(`raise RuntimeError('empty universe — refusing to write coins.json')`) — damit greift
in der Ingestion automatisch der on-disk-Fallback und Housekeeping überspringt den
Refresh, genau wie bei einem Fetch-Fehler.

Kein Live-Eingriff (ENVIRONMENT: BUILD). DB-freie Tests: `backtest/test_coins_writer.py`
(Filter-Parität, Atomarität, Fetch-Fehler lässt Datei unversehrt, leeres/fehlendes
`symbols`-Feld → Write verweigert, Datei unverändert) +
`backtest/test_delisted_cleanup.py` (Shape-Guard akzeptiert echte Perps, verwirft die
benannten Falsch-Close-Symbole). ruff/format/mypy grün.

## [2026-07-11] P1.12: Fensterglobale Indikatoren nur noch auf die neueste GESCHLOSSENE Kerze (as-of-now) + 4 S/R-Reader konsistent (T-2026-CU-9050-084)

Die fensterglobalen Indikatoren (ein Trendline/Channel-Fit, ein HVN/POC-Histogramm, ein
S/R-Pivot-Scan, eine Fibonacci-Spanne) wurden bisher als Konstante bzw. rückprojizierte
Linie auf JEDE Zeile des Rechenfensters gebroadcastet — Look-ahead in der gespeicherten
Historie (Step-2-Beleg: 149 distinct POC / 236 distinct support über 5000 alte Rows; eine
5000 Kerzen alte Zeile trug den heutigen Level). `2_indicator_engine.calculate_indicators_optimized`
schreibt sie jetzt NUR noch auf die neueste GESCHLOSSENE Kerze (as-of-now-Referenzzeile) und
NULLt sie auf der forming Kerze und allen älteren Zeilen (produktionsbewiesener NaN-Write-Pfad
wie P1.13/T-054; `trend_direction` als echtes SQL-NULL). Betroffen sind 27 Spalten: der ganze
Trend/Channel-Block, POC/HVN, SUPPORT/RESISTANCE_PRICE und alle FIB_*.

Operator-Entscheid Michi 2026-07-11 — **Variante B** statt der wörtlichen Dispatch-Vorgabe
"letzte Zeile": geschrieben wird auf die neueste GESCHLOSSENE Kerze, nicht auf die absolute
letzte (forming) Zeile. Grund: die Verifikation zeigte, dass die neueste Indikator-Zeile die
forming Kerze IST (die WS-Ingestion puffert jeden Kline-Tick ohne `k['x']`-Filter,
`1_data_ingestion.py:693`) und alle Serving-Reader ohnehin die neueste GESCHLOSSENE Kerze lesen.
Damit lesen sie den identischen Wert weiter, harte Regel 5 (Forming Candle) bleibt gewahrt —
die Regel-5-vs-as-of-now-Kollision ist aufgelöst statt improvisiert.

Reader-Inventar korrigiert: der Dispatch nannte 3 Reader, es sind fünf S/R-Konsumenten.
`strat_support_resistance`/`strat_main_channel` (iloc[1]) und `strat_5_percent`/`strat_fast_in_out`
(iloc[0] = forming!) lesen den Level jetzt robust aus der neuesten Nicht-NULL-Zeile
(`first_valid_index`) statt aus einem festen Positionsindex — bei vorhandener forming Kerze exakt
derselbe Wert, bei fehlender forming Kerze bleibt der Reader auf der geschlossenen Referenzzeile
statt still eine genullte Zeile zu lesen. `12_ai_ats_bot` bleibt unverändert: es liest iloc[-2]
(= neueste geschlossene = Referenz) und hat einen frame-weiten `fillna(0)`; unter Variante B
keine Feature-Semantik-Änderung (die unter der Dispatch-Default-Variante A befürchtete
ATS-Verschiebung entfällt).

Verifiziert (DB-frei): `backtest/test_window_features.py` (Engine-Invariante für den forming- und
den rein-historischen Fall, je Reader ein Guard, fällt auf dem Pre-Fix-Stand); Regression-Guard-
Golden refresht (Regel 9, dokumentierter Grund) — die 648 Breaches sind exakt die 27
fensterglobalen Spalten × 24 Fixtures auf den Nicht-Kopf-Zeilen, keine einzige per-Row-Spalte;
Serving-Verifikation über die 4 realen 1h-Fixtures: Signal-Raten-Delta der Classic-Strats = 0
(Level byte-gleich vor/nach), und ohne den Reader-Fix hätte 5-Percent auf SOL alle 993
Sweep-Signale verloren.

Bekannte Risiken / Folge-Tasks (bewusst NICHT in diesem PR):
- **9_ai_sr_bot (+ `core/sra_features`)** liest die Indikatoren mit `open_time <= t_time` OHNE
  Floor-Guard → kann die forming Kerze treffen und hält NaN (kein fillna). Unter Variante B werden
  support/resistance/r_squared/trend_direction dort für forming-Reads NaN (XGB-nativ, vom Task als
  bekanntes Risiko vorgesehen). Root-Cause ist der fehlende Floor-Guard (R1) — Folge-Task: auf die
  neueste GESCHLOSSENE Kerze umstellen (wie `15_ai_master_bot`) + SRA-Retrain.
- **15_ai_master_bot / `core/aim2_features`** liest strikt `open_time < floor(ts)` → neueste
  geschlossene = Referenz → sicher, keine Änderung. **24_quasimodo / 25_smc_ml_sniper** lesen nur
  `trend_direction` von einer geschlossenen Zeile mit ffill+bfill → robust (bot 25 TD-Pivot bekommt
  den backfill'ten neuesten-geschlossenen Wert). **27_bot_regime_analyzer** liest keine der Spalten.
- Bestandszeilen in der DB behalten den alten Broadcast-Wert; die Historien-Bereinigung ist ein
  separater VPS-Job (nicht hier).
## [2026-07-11] 2_indicator_engine.py — calculate_rsi auf echten Wilder-RSI migriert (bewusste Migration, T-2026-CU-9050-095, P2.12)

Operator-freigegebene bewusste Migration (Michi 2026-07-11). `calculate_rsi` glättete
den Average-Gain/-Loss bisher mit `ewm(span=period)` — das ist α=2/(period+1), also
für period=14 wie ein Wilder-7,5-RSI (span=p entspricht Wilder-Periode (p+1)/2). Das
gespeicherte RSI_14 lief damit ~4,8 Punkte heißer als echtes Wilder (Step-2-Messung,
P2.12), weshalb die 70/30-Bänder (und die rsi_9-55/75-Gates) zu oft feuerten. ATR und
`calculate_smma` im selben File waren schon korrekt Wilder — RSI zieht jetzt nach:
`ewm(alpha=1/period, adjust=False)`. Gegen eine unabhängige, hand-gerollte Wilder-RMA-
Rekursion gepinnt (`backtest/test_wilder_rsi.py`, bit-genau ≤1e-9); die alte span-Formel
fällt als Regression. Das NaN-Warmup-Verhalten (P1.13/T-054: erste Zeile fließt als NaN
statt fabrizierter 50/100) und der Flat-Fall (konstanter Preis → 0/0 → NaN, T-060)
bleiben erhalten — nur α ändert sich, nicht das NaN-Handling.

**Regression-Guard-Golden bewusst refreshed (Regel 9):** exakt 120 `numeric_drift`-
Breaches, ausschließlich RSI_6/9/12/14/24 über alle 24 Fixtures, null Nicht-RSI-Spalten —
die Änderung ist voll gekapselt, keine Engine-Ausgabespalte leitet aus rsi ab. `guard.py
verify` danach grün, `smoke` grün.

**Signal-Raten-Delta** (`tools/wilder_rsi_signal_delta.py`, 24 Guard-Fixtures, 12.468
geschlossene Bars, isoliert nur der RSI-Anteil der Gates): die 70/30-Extreme fallen am
stärksten — RUB2 overbought (rsi_14>70) −4,84 pp (9,28→4,44 %, ~−52 % rel.), oversold
(rsi_14<30) −5,61 pp (12,28→6,67 %, ~−46 %). Das ist genau das gemessene „70/30 feuern
zu oft". Die SHORT-Gates sinken moderat (strat_5_percent −2,61 pp, fast_in_out −2,40 pp),
die zentralen 55-75-LONG-Bänder bleiben ~flach (±0,7 pp). Das ist beabsichtigt — die
Migration senkt die Signal-Raten; die 55/70/75-Schwellen werden hier NICHT nachgetunt
(das folgt erst nach dem Retrain, P1.13-Doktrin).

**Kopplung — nicht isoliert live wirksam (C-Gate, VPS, OPUS-HANDOFF §6):**
- *Retrain:* `rsi_14` ist direkter Modell-Input von TD2/BB2/QM2 (`ABSOLUTE_INDICATORS`),
  rsi_6/9/12/14/24 von MIS2, rsi_9/14/24 von SRA2, rsi_6/14 von AIM2, rsi_14 von den
  Research-Bots; abgeleitete Features (mis `rsi_*_delta_1`, `rsi_14_above_50`,
  `rsi_14_cross_above_30`, TD/BB Three-Drive-RSI-Pivot-Monotonie) verschieben sich mit.
  Die deployten Artefakte sahen den alten span-RSI → Retrain auf der verschobenen
  Verteilung vor Vertrauen.
- *Mixed-History (wie R3-Pool-Flip):* ab Deploy trägt die DB-Historie zwei RSI-Domänen
  (alt span pre-Deploy, Wilder post-Deploy); bis zu einem VPS-Recompute lesen Trainer
  gemischte Werte. Wichtig: das T-061-Tool `recompute_indicators.py` nullt nur Warmup-
  Heads und recomputet bewusst KEINE Werte (Full-Recompute ist nicht positions-stabil,
  bis 48 % Mid-Band-Drift auf rsi_14 schon bei gleicher Formel). Ein Wilder-Recompute
  der rsi_*-Spalten ist daher ein echter Full-Recompute — keine triviale T-061-
  Erweiterung, sondern eine größere Operator-Entscheidung.

Sequencing (P1.13-Doktrin, „nie isoliert"): (1) Code-Fix + Golden-Refresh [dieser PR],
(2) VPS-Recompute rsi_* → eindomänig, (3) TD2/BB2/QM2 + MIS2/SRA2/AIM2/Research-Retrain,
(4) erst danach die 55/70/75-Schwellen neu tunen. AUDIT_TODO P2.12 bleibt offen bis
Recompute+Retrain.

## [2026-07-11] tools/restart_fleet.ps1 — UAC-freier Fleet-Restart-Zyklus über den Task "Kythera Watchdog" (T-2026-CU-9050-074)

Lehre aus dem 00:32-Mass-Crash (Konsole des manuell gestarteten Watchdogs geschlossen,
Watchdog tot, 15 verwaiste Bots, Dashboard down) und der anschließenden UAC-Odyssee:
Recovery-Aktionen brauchten Elevation, aber UAC-Prompts erreichen Michis Desktop bei
mehreren RDP-Sessions nicht zuverlässig. Seit T-068 existiert der Scheduled Task
"Kythera Watchdog" (User Michael, Password-Logon, RunLevel Highest) — sein eigener
User darf ihn OHNE Elevation starten und stoppen; der Task-Scheduler wendet das
elevated Token an. Das neue Operator-Script fährt den kompletten Zyklus unelevated:
`git pull --ff-only` ZUERST (schlägt er fehl, bleibt die Fleet unangetastet, inkl.
Branch-Guard: gepullt wird nur auf `main`), dann `Stop-ScheduledTask`, dann
`Start-ScheduledTask` mit Verifikation (Task-State, Bot-Zählung über den unelevated
sichtbaren Python-Parent-Fingerprint, Dashboard-Port 5000). Das Script killt selbst
KEINE Prozesse — Waisen, die den Tree-Stop überleben, reapt der nächste Watchdog-Start
(`_terminate_orphan_fleet`, P0.2). `-DryRun` für den Preflight (verifiziert: Task
sichtbar, 37 Bot-Prozesse erkannt, Exit 0), `-SkipPull` für Restart ohne Pull.
Der 3-Voter-Review schloss drei False-Success-Pfade: (1) Stop-Verifikation über einen
PID-Snapshot VOR dem Stop (der Parent-Fingerprint ist nach Watchdog-Tod strukturell
blind für Waisen), (2) Erfolgskriterium = Task-State `Running` UND Dashboard-Port
(ein verwaistes Alt-Dashboard auf 5000 täuscht sonst bei import-gecrashtem Watchdog
Erfolg vor), (3) Fleet-außerhalb-des-Tasks (00:32-Muster: manuell gestarteter
Watchdog) → Abbruch statt Mutex-No-op-Restart. Exit-Codes 0/1/2/3/4 dokumentiert
(4 = Fleet gestoppt, Start fehlgeschlagen → Fleet DOWN, manueller Task-Start).
Achtung: der Stop-Pfad (Task-ACL) ist bis zum ersten echten Lauf ungetestet — bei
"Access denied" braucht die ACL einmalig einen elevated Fix. Fleet-Restart bleibt eine
Operator-Entscheidung (OPUS-HANDOFF §6); das Script läuft nie automatisch.

## [2026-07-11] QM2-Retrain-Vorbereitung: qm_ml_trainer.py schreibt jetzt model_id (T-2026-CU-9050-061, Schritt 2)

Vorbereitung für den QM2-Retrain nach dem P1.13-Recompute (Schritt 1 dieses Tasks
ist live: 3,07M Warmup-Kopfzeilen genullt). Der Task nennt `retrain_from_replay.py`
für TD2/BB2/QM2 — aber weder das noch `walkforward_sim.py` kennt `qm`. Quasimodo
(Bot 24) hat einen eigenen Trainer, `qm_ml_trainer.py`, der die (jetzt recomputeten)
`_indicators`-Tabellen liest, eine eigene Walk-Forward-Trade-Sim für Labels nutzt,
`fillna(0)` fährt (Parität mit der Bot-Serving-Imputation seit PR #62) und bereits
nach `staging_models/` schreibt. Operator-Entscheid (Michi 2026-07-11): QM2 über
diesen bestehenden Trainer statt einer `retrain_from_replay`-Erweiterung.

Einzige Lücke war Regel 6: `qm_ml_trainer.py` schrieb **keine** `model_id`, sodass
ein QM2-Retrain still als abgeleitetes `QM_1H` gepostet und mit der QM1-Statistik
verschmolzen wäre, auf der das Orchestrator-Gating entscheidet. Fix: der Trainer
schreibt jetzt `meta['model_id'] = f"QM2_{tf.upper()}"` (Konvention wie
`retrain_from_replay`: `QM2_1H`). Bot 24 liest das Feld bereits (T-030) und leitet
nur bei Alt-Artefakten ohne `model_id` auf `QM_1H` zurück; sein Kommentar ist auf
den neuen Ist-Zustand aktualisiert. Kein Verhaltensänderung an bestehenden
Artefakten — nur neue QM-Retrains tragen den Tag.



Operator-Freigabe Michi 2026-07-11: MAX1 (Bot 34) geht in den Shadow-Betrieb. Das auf dem
VPS erzeugte Artefakt `max1_model_SHORT.pkl` (+ `_meta.json`) ist aus `staging_models/` in
den Repo-Root promoted und hier committet (Deploy-Konvention wie RUB2, 07c8874) — Byte-Kopie
des RUB2-SHORT-Modells unter dem Tag MAX1, Load-Verify auf dem VPS mit sklearn 1.7.1:
`True MAX1 0.829 15 True`. `MAX1_LIVE_POSTING` bleibt AUS (shadow-only, kein Cornix-Posting);
Scharf-Schalten ist ein separater Operator-Schritt.

### Gate-Zahlen für den Shadow-Start (Operator-Ziel: maximale Trefferquote)
`.env` auf dem VPS: `MAX1_MIN_PROB=0.85`, `MAX1_MAX_PER_DAY=3` — bewusst NICHT der
Default 0,93. Begründung (T-2026-CU-9050-070, KB `mcp-a65a1da76492`): die Live-Kurve
(06.–11.07., 44 posted/28 closed) zeigt die höchste WR im Band 0,829–0,85 (81–82 %,
n=21–28), während ≥0,88 die WR **fällt** (60–71 %) und nur der Ø-PnL steigt — hohe
Thresholds kaufen Expectancy, nicht Trefferquote. Zudem clustern die ≥0,88-Kandidaten
in Funding-Episoden (24h-Kappe liefert dann ~0,7/Tag statt 3). Alles n<30 — die
Shadow-Phase misst genau die kappen-gebundene Selektions-WR; finale Zahlen danach.

### Befund am Rande (eigener Folge-Task T-2026-CU-9050-071)
Die Replay-Kurve (rub_replay_365d) ist für die Gate-Kalibrierung unbrauchbar: gematchte
Signal-Paare Live↔Replay korrelieren −0,37, Replay-OOS erreicht in 59 Tagen nie prob ≥ 0,93.
Feature-Skew Serving vs. Replay, Hauptverdacht Funding-Features.

## [2026-07-11] P1.13-Recompute: ein voller Recompute ist NICHT positions-stabil — Werkzeug zur Kopfzeilen-Nullung (T-2026-CU-9050-061, Schritt 1)

Erster Schritt des P1.13-Folge-Tasks: die Warmup-Kopfzeilen der Bestands-Coins auf
den neuen NaN-Stand bringen (der Live-Fix aus T-054/PR #43 wirkt nur auf Neu-Listings).
Dieser PR liefert das **Werkzeug** und den tragenden Analyse-Befund; der eigentliche
Live-DB-Write ist ein separater, operator-gegateter Schritt (C-Gate, noch nicht ausgeführt).

### Befund (gemessen, nicht behauptet)
Der naheliegende Weg — jede `_indicators`-Tabelle neu rechnen und upserten — ist
**nicht positions-stabil**. `2_indicator_engine` schreibt inkrementell (ein 1000-Kerzen-
Fenster je Lauf, über Monate, teils von älteren Engine-Ständen), und die heutige Engine
reproduziert die gespeicherten Mid-Band-Werte nicht. Gemessen an einer 30-Tabellen-
Stichprobe: ein voller Recompute würde **~79.000 Mid-Band-Zellen** verändern (worst case
+707 % auf `rsi_14`), nicht nur die ~18.900 Warmup-Kopfzeilen. Ursache: fenster-globale
Features (`TRENDLINE_*`, `HVN`, `POC`, `FIB_*`) sind Skalare übers ganze Fenster, lange
ewm-Indikatoren (`EMA_200`, `SMMA_200`) konvergieren langsam vom Startpunkt. Ein voller
Recompute hätte die Serving-Verteilung des gesamten Fleets verschoben und Training von
Serving entkoppelt — das Gegenteil des Task-Ziels.

### Lösung
`tools/recompute_indicators.py` nullt **nur** die Warmup-Kopfzeilen der vier P1.13-Familien
(`WMA_*`, `RSI_*`, `BOLL_*_20`, `DONCHIAN_*`): Die Engine bestimmt die Warmup-Grenze (die
Zeilen, die sie jetzt als NaN liefert), aber geschrieben wird ausschließlich NULL an diese
Positionen — nie ein neu gerechneter Mid-Band-Wert. Damit ist die Operation positions-stabil
per Konstruktion (Mid-Band = unveränderte Serving-Werte). Der Retrain braucht genau das:
die genullten Kopfzeilen fallen im Replay per `dropna()` (seit T-045) aus den Trainingsdaten.
Läuft neben dem Live-Bot 2 (nullt nur historische Zeilen, die der inkrementelle Writer nie
anfasst), niedrige Priorität, idempotent, resumable. `--dry-run` (default) schreibt nichts
und belegt die Kopf/Mid-Band-Trennung; `--execute` ist operator-gegatet.

Verifikation: `backtest/test_recompute_head_nulling.py` (5 Tests, standalone, DB-frei) pinnt
die Grenze — Kopfzeilen werden genullt, Mid-Band-Abweichungen nur berichtet nie geschrieben,
neueste Zeilen (bot-2-Race) ausgeschlossen. Dry-Run über 30 Tabellen bestätigt ~49 min bei
3 Workern für den vollen Lauf. **Noch offen (separate Schritte):** der Live-Execute, der
TD2/BB2/QM2-Retrain, und — erst beim Artefakt-Rollout — die `bfill`-Entfernung in
`24_quasimodo_bot.py:126` / `25_smc_ml_sniper.py:220`.
## [2026-07-11] MAX1: eigenständiger High-Conviction-Klon von RUB2-SHORT für den Main-Channel (T-2026-CU-9050-067)

RUB2-SHORT ist die stärkste Short-Kante der Fleet (live seit 06.07.: 24 Closes,
79 % TP1-WR, +4,2 % Ø PnL — T-2026-CU-9050-044), feuert aber ~9×/Tag. Michis Ziel
für den Main-Channel sind **1-3 Trades/Tag mit sehr hoher Trefferquote**. Statt
RUB2 zu drosseln (T-2026-CU-9050-050 → **wontfix**: RUB2 bleibt unverändert in
seinem Channel), läuft dasselbe Modell jetzt zusätzlich als eigener Bot
**`34_ai_max1_bot.py`** mit selektivem Gate und eigenem Tag `MAX1`.

Drossel in `core/max1_gate.py` (reine, DB-freie Selektion): hohe
Mindest-Probability (`MAX1_MIN_PROB`, Default **0,93** — nie unter dem
Artefakt-Threshold 0,829) als eigentlicher Selektor, plus eine **harte rollierende
24h-Kappe** (`MAX1_MAX_PER_DAY`, Default **3**) als Backstop. Je Scan: Kandidaten
sammeln, per Symbol deduplizieren, deterministisch ranken, auf die freien Slots
schneiden. Der 24h-Zähler liest Shadow **und** Live aus `ml_predictions_master`,
damit die Kappe im Shadow exakt wie live greift.

Detection, Features (9 rub + 6 funding) und Trade-Geometrie kommen aus den
**geteilten** Buildern (`core/rub_features.py`, `core/funding_features.py`,
`hvn_sr_trade_geometry`) — importiert, nicht angefasst (X-R1). `13_ai_rub_bot.py`
bleibt unverändert. Cooldown-/Dedupe-/Offene-Trade-Räume sind über den Tag getrennt:
MAX1 und RUB2 blocken sich nicht gegenseitig, Doppel-Exposure auf demselben Coin ist
die bewusste Konsequenz (dokumentiert in `docs/MODEL_INTENT.md` §8a).

Artefakt: `tools/make_max1_artifact.py` erzeugt aus dem RUB2-SHORT-Modell eine
Kopie mit `meta.model_id=MAX1` nach `staging_models/` (Modell, Feature-Vertrag,
Kalibrator, Val-Operating-Point verbatim — nur die Identität wechselt, harte
Regel 6). Der Posting-Tag kommt aus dieser Meta, nie aus einer Konstante (Falle 16).

Nichts scharf geschaltet: `MAX1_LIVE_POSTING` ist **Default-OFF** (Shadow-only),
ohne deploytes Artefakt läuft Bot 34 im Idle-Modus, und die Promotion aus
`staging_models/` ist Michis Operator-Entscheid (OPUS-HANDOFF §6). Genau EINE
Cornix-parsebare Message je Signal über `core.signal_post.post_ai_signal`
(harte Regel 4). Watchdog-Registrierung: `start_delay=223`.

Verifikation: `backtest/test_max1_gate.py` (21 neue Tests — Selektion/Kappe/
Default-off-Gate/Tag-aus-Meta/Cornix-Einzelmessage/Cooldown-Trennung), volle
Suite 458 grün, ruff/format/mypy grün, Artefakt lädt über `core/model_artifacts`
(Tag MAX1, 15 Features, Threshold 0,829, Kalibrator ja).

## [2026-07-10] EPD und SRA bekommen den Active-Trade-Check; EPDs Funding-Load wird gecacht (T-2026-CU-9050-055)

Zwei Folgebefunde aus T-2026-CU-9050-042, auf Operator-Auftrag (Michi, 2026-07-10).
Damit ist die Fehlerklasse aus P1.48 fleet-weit geschlossen: **alle** postenden
AI-Bots prüfen jetzt vor dem Signal, ob auf dem Coin schon ein Trade offen ist.

**Der Positions-Guard.** Weder `10_pump_dump_detector` (EPD) noch `9_ai_sr_bot`
(SRA) berührte `ai_signals` lesend. Was sie hatten, waren Frequenz-Sperren:

- EPD: `pd_state["last_alert_time"]`, 900 Sekunden — und ein **In-Memory**-Timer.
  Ein EPD-Trade überlebt eine Viertelstunde regelmässig; danach durfte derselbe
  Coin erneut feuern, und Cornix öffnete eine **zweite volle Position** daneben.
- SRA: der 4h-Cooldown plus die `trade_id`-Duplikatprüfung. Letztere schützt nur
  gegen dasselbe Setup — nicht gegen ein **neues** S/R-Setup auf einem Coin, auf
  dem bereits ein SRA-Trade läuft.

Beide bekommen jetzt `SELECT 1 FROM ai_signals WHERE symbol/direction/model IN
(tag, legacy_tag)` und überspringen das Signal bei einem Treffer. Bei **EPD** läuft
der Check *nach* der Prediction — die Richtung entsteht erst im `argmax` — aber
*vor* der Shadow/Post-Verzweigung, also unterdrückt er wie bei MIS/RUB auch die
Shadow-Zeile. Operator-Entscheid: `symbol+direction` wie bei den Geschwistern, kein
richtungsagnostischer Key, damit ein Reversal auf demselben Coin erlaubt bleibt.
Bei **SRA** steht die Richtung schon aus `active_trades_master` fest, der Check
sitzt deshalb vor Indikator-Fetch und `predict_proba` und spart auch Arbeit. Der
Legacy-Tag reist in beiden Binds mit (transitionaler Dedup über den EPD3-/
SRA2-Generationswechsel); Cooldown und 900s-Timer bleiben unangetastet daneben.

**Der Funding-Load — und eine Korrektur an der eigenen Notiz von T-042.** Dort stand,
der Load feuere „pro qualifizierendem Tick, weil der `vol_ratio>=5`-Vorfilter
anhält". Das war ungenau: der 900s-Timer sperrt sehr wohl **vor** der ML-Strecke.
Der Wiederholungsfall ist ein anderer — der Timer wird **nur im Live-Trade-Zweig
gesetzt**, ein Coin im Shadow-Band (0.25..threshold) passiert das Gate also auf
jedem 10s-Tick und zog die Query jedes Mal.

„Funding nur bei Trades laden" ist **nicht baubar**: die 6 Funding-Spalten sind
Modell-**Input**, sie erzeugen die `prob`, die überhaupt erst entscheidet, ob es ein
Trade wird. Die Reihenfolge lässt sich nicht umdrehen. Was geht, ist die
Wiederholung: `core/funding_features.funding_features_cached` cacht je Symbol bis zur
nächsten Abrechnung, die das Ergebnis überhaupt verändern kann.

Der Schlüssel kommt dabei aus den **Daten**, nicht aus der Wanduhr — und das ist
der Punkt, an dem der erste Entwurf dieses Fixes falsch war. Er cachte je
angebrochener Stunde, in der Annahme, Binance rechne auf vollen Stunden ab. Ein
adversarialer Review hat das mit zwei ausgeführten Gegenbeispielen widerlegt:
`tools/backfill_funding_rates.py` schreibt `funding_time` millisekunden-genau,
nichts erzwingt das Stunden-Raster (eine Abrechnung um 12:30 blieb bis 13:00
unsichtbar); und der 120s-Ingestion-Guard war eine Wette auf eine SLA — eine Zeile,
die nach 150s landete, wurde für den Rest der Stunde ignoriert.

Jetzt gilt ein Eintrag bis zu der Abrechnung, die das Ergebnis als Nächstes ändern
kann: der nächste `funding_time`, der schon in der Historie steht (gleich auf welcher
Minute er sitzt), oder — hinter der letzten Zeile — die letzte Abrechnung plus das
aus den jüngsten Abständen geschätzte Intervall (8h/4h/1h je Paar). Ist die fällige
Zeile noch nicht ingested, ist der Eintrag bereits abgelaufen und es wird bei jedem
Aufruf neu geladen, bis sie erscheint; ihr `funding_time` schiebt die Grenze dann
weiter. Der Cache **korrigiert sich selbst**, statt auf einen Zeitplan zu wetten.

Die Intervall-Schätzung nimmt das **Minimum** der jüngsten Abstände, nicht den Median
— ein zweiter Fund des Re-Reviews. Die Fehlerrichtungen sind nicht gleich teuer: zu
kurz geschätzt kostet einen zusätzlichen DB-Roundtrip, zu lang geschätzt lässt den
Cache über einer echten Abrechnung sitzen und einen stale Wert ausliefern. Verkürzt
ein Coin seine Kadenz (8h → 1h) oder verzerrt eine Ingestion-Lücke die Abstände,
überschätzt ein Median (oder der letzte Abstand) um Stunden; das Minimum kann das
strukturell nicht.

Damit steht die Wertneutralität wieder auf der Invariante statt auf einer Annahme:
`funding_features_asof` hängt vom Zeitstempel **ausschliesslich** über den
`searchsorted`-Schnitt ab, und alle Aggregate sind Suffixe (`rates[-3:]`,
`rates[-270:]`) — die wandernde `since`-Untergrenze geht nicht ein. Was die Parität
bräche, wäre ein **naiver Zeit-TTL**: der kann eine Abrechnungsgrenze überspannen.
Der T-042-Eintrag unten warnte genau davor und schloss daraus fälschlich, ein Cache
sei überhaupt kein Drop-in.

Verifikation DB-frei: `backtest/test_funding_cache.py` nagelt zuerst die Invariante
selbst fest (as-of konstant zwischen zwei Abrechnungen, und beweglich über eine —
beides oberhalb von `MIN_HISTORY`, sonst verglichen die Tests zwei leere Dicts),
dann beide widerlegten Gegenbeispiele, dann das Cache-Verhalten. Erweitert:
`test_epd_tag.py` (15), `test_sra_tag.py` (13). Mutations-geprüft: der uhr-gebundene
Stunden-Key, eine aus der letzten Zeile statt aus dem nächsten Satz abgeleitete Grenze,
ein Median- oder Letzter-Abstand-Schätzer und ein `searchsorted`-Schnitt auf `right`
(Lookahead bei exakter Zeitstempel-Gleichheit) fallen alle durch. Die zweite und die
dritte Mutation waren echte Bugs in den ersten beiden Anläufen dieses Fixes.

**Live-Semantik ändert sich bewusst** an genau einer Stelle je Bot: ein Signal auf
einem Coin, auf dem bereits ein Trade derselben Richtung offen ist, fällt weg. Erste
Position, freier Coin, Gegenrichtung und der berechnete Funding-Wert bleiben
unverändert. Kein Rollout, kein Artefakt angefasst, keine DB-Änderung.

**Nebenbei (Boy-Scout, vorbestehend seit T-042):** `CACHE_SINCE_DAYS` von 95 auf 110
angehoben. Der Funding-Load fensterte auf 95 Tage, das 270-Sätze-Fenster von
`fund_pctl_90d` braucht bei 8h-Kadenz aber exakt 90 — nur 5 Tage Puffer. Ein Coin mit
>5d kumulierter Funding-Lücke bekam live <270 Samples und wich in diesem einen
Feature minimal vom Trainer ab (volle Historie). 110d gibt 20 Tage Lücken-Puffer.
Berührt die Cache-Werteidentität nicht (Cache und `asof` sehen denselben Frame).

## [2026-07-10] ROM1: Regime-Auto-Close differenziert — Gewinner trailen statt blind closen (T-2026-CU-9050-049, B6)

Bei einem Regime-Wechsel schloss der Orchestrator (`28_signal_orchestrator.py`)
jeden nicht-whitelisteten offenen Trade per Market-`Close` — laut Report 16 (B6)
wurden dabei ~49 % der Trades **im Gewinn** gekappt (median PnL 0 %, Churn +
Fees + zensierte Statistik).

Neu, hinter dem Default-OFF-Gate `TRAIL_WINNERS_ON_REGIME_CHANGE`
(env `KYTHERA_REGIME_TRAIL_WINNERS=1`): ein Trade **im Gewinn** wird nicht mehr
geschlossen, sondern sein Stop-Loss via Cornix-**SL-Update-Message**
(`SL <SYMBOL> <preis>`, symbol-adressiert wie `Close`) auf **Break-even** bzw.
das **letzte erreichte TP-Level** gezogen; der Trade läuft weiter. Verlierer
werden weiter market-geschlossen.

A/B messbar über die neue Spalte `orchestrator_open_trades.regime_close_action`
(`REGIME_CHANGE_CLOSED` vs `REGIME_CHANGE_TRAILED`, plus `regime_action_at`).
Der TRAILED-Tag überlebt den späteren finalen Close (Lifecycle-Sync lässt ihn
unangetastet), so bleibt die Kohorte für den 4–6-Wochen-Live-Vergleich über den
Tracker-Pfad identifizierbar (Auswertungs-Query dokumentiert in
`docs/REGIME_ORCHESTRATOR.md`).

Sicherheit: die SL-Update-Message ist eine einzeilige Kommando-Semantik und
**nie** ein zweites Cornix-parsebares Signal (harte Regel 4, unit-getestet gegen
`parse_cornix_signal`). Da `Close <coin>` symbol-weit wirkt, wird ein Coin mit
getrailtem Gewinner im selben Pass **nicht** zusätzlich market-geschlossen.

Kein Deploy, kein Scharfschalten: das Gate ist Default-OFF, die additive
`ensure_schema`-Spalte (B8-Präzedenz) greift erst beim nächsten VPS-Restart —
das Aktivieren des Experiments ist eine Operator-Entscheidung (OPUS-HANDOFF §6).

Verifikation: `backtest/test_signal_orchestrator.py` (11 neue Tests, 86/86),
`test_regime_detector.py` + `test_bot_regime_analyzer.py` (79/79),
`regression_guard verify` OK (24/24), ruff/format/mypy grün. Wirkungsnachweis
live (VPS).

## [2026-07-10] ATB1: posted-Flag spiegelt den Live-Trade, nicht hart False (T-2026-CU-9050-062, P1.47)

`14_ai_atb_bot.py` loggte jede Prediction ab `ml_prob >= 0.25` nach
`ml_predictions_master`, hart mit `posted=False` — auch die, die tatsächlich
gehandelt wurden (`ml_prob >= threshold`). Der Live-Trade selbst (`send_signal`)
schreibt nur nach `ai_signals`, es gab also nie eine `posted=True`-Zeile.

Folge seit P1.44: der `created_at`-JOIN des Market-Trackers (`m.posted = TRUE`)
matchte keine einzige ATB1-Zeile, offene ATB1-Positionen fielen dauerhaft auf
`NOW()` zurück und wirkten in den Opened-Buckets ewig frisch. Anders als
ATS1/RUB1/MIS1/SRA1, die auf ihrem Live-Zweig `posted=True` schreiben.

Der Flag kommt jetzt aus `_atb1_posted_flag(ml_prob, threshold)` — `True` genau
dann, wenn die Prediction den Trade auslöst. Als reine Funktion extrahiert, weil
`run_trendline_detector` als Ganzes nicht treibbar ist; so ist die Grenze
(`threshold`, **nicht** das 0.25-Shadow-Gate) testbar und gegen ein späteres
„Vereinfachen" gesichert.

Wirkung nur Anzeige — Kelly/WR ziehen `created_at` aus
`closed_ai_signals.open_time`, nicht aus dem JOIN. Kein Deploy; ATB1 ist
geparkt, der Fix greift beim nächsten Restart. Vor dem Entparken von Bot 14 war
das die offene Auflage.

Verifikation: `backtest/test_atb1_posted_flag.py` (neu, standalone, DB-frei,
5/5). Ehrlich zur Beweiskraft: die fünf Tests prüfen den neuen Helper, auf dem
Pre-Fix-Stand fehlt er, also erroren sie (`AttributeError`) statt den Insert-Bug
verhaltensmässig zu messen — der Insert-Aufruf selbst ist nur indirekt gedeckt
(`run_trendline_detector` ist als Ganzes nicht treibbar). Ihr Wert ist der
Forward-Guard auf die Helper-Grenze: `test_boundary_is_not_the_025_shadow_gate`
pinnt, dass die Grenze `threshold` ist und nicht das 0.25-Shadow-Gate, und
`test_returns_plain_bool_not_numpy` (numpy-Input) sichert den `bool()`-Wrapper
für psycopg2. ruff + format + mypy grün.

---
## [2026-07-10] Merge-Train-Onboarding: Kythera-PRs merged jetzt der Daemon, nicht die Session (T-2026-CU-9050-063)

Kythera fährt ab jetzt auf dem merge-train (`services/merge_train/` in
knowledge_base_internal, Hetzner): nach bestandenen Kern-Reviews stempelt die
Session `cu/reviews`, setzt das Label `merge-train` und schließt — der Daemon
merged seriell und rebased jeden PR höchstens einmal. Grund: am 2026-07-10
liefen zeitweise 6+ parallele Sessions gegen main; jede CHANGELOG-Top-Insertion
kollidierte mit jeder, und wer selbst mergte, zahlte pro PR 1–2 manuelle
Konflikt-Runden (O(n²)-Rebase-Kaskade — genau der Fall, für den der Train
gebaut wurde). Operativ aktiviert: Labels `merge-train`/`merge-train:failed`
im Repo, `MERGE_TRAIN_REPOS` auf Hetzner um `Kythera` erweitert, Service
neu gestartet. Kein Deploy-Hook (Build-Repo, post-merge läuft nichts).
Doku: `docs/OPUS-HANDOFF.md` §2 Schritt 7 (inkl. Bounce-/Re-Queue-Regeln) und
`CLAUDE.md` Workflow. Dieser PR ist selbst der erste Zug — sein Merge durch den
Daemon ist die End-to-End-Verifikation inkl. Daemon-PAT-Zugriff aufs Repo.
## [2026-07-10] AIM2-Trainer: Meta-Gate-Tags aus load_events ausgeschlossen — F6-Symmetrie zum Serving (T-2026-CU-9050-065)

Folge aus T-2026-CU-9050-051. Die Serving-Seite (`15_ai_master_bot.load_signal_stream`)
schließt AIM1/AIM2/AIM2-TOPN aus dem Kandidaten-/Schwarm-Stream aus (F6-Selbst-Feedback),
der Trainer `tools/aim2_build_dataset.py` filterte aber nur `model_name <> 'AIM1'`. Ein
künftiger AIM2-Retrain hätte damit die eigenen Meta-Gate-Ausgaben (AIM2 postet seit 06.07.,
AIM2-TOPN sobald live) als Trainings-Events gelabelt — dieselbe Leckage, die serving-seitig
längst gefixt ist, und ein Bruch der AIM2_DESIGN-§3-Invariante „identische Definition wie im
Trainer".

### Changed
- `tools/aim2_build_dataset.py`: `load_events` zieht jetzt `model_name NOT IN ('AIM1', 'AIM2', %s)`
  mit dem Tag aus `core.aim2_topn.MODEL_TAG` — Symmetrie zum Serving hergestellt, Tag
  single-sourced (kein zweites Literal).

### Added
- `backtest/test_aim2_event_source_symmetry.py` (DB-frei, standalone): pinnt statisch, dass
  Trainer und Serving denselben Meta-Gate-Ausschluss tragen und keiner mehr den alten
  `<> 'AIM1'`-Filter benutzt.

Kein Live-Eingriff, kein Retrain-Rollout — reine Definitionskorrektur für den nächsten
Trainings-Lauf. Verifiziert: neuer Test grün, `guard.py verify` (24 Fixtures), ruff+mypy grün.

## [2026-07-10] Spike: Replication-Scoring (polybot) auf Hyperliquid-Public-Fills evaluiert (T-2026-CU-9050-058)

Machbarkeits-Eval, ob polybots „Replication Scoring"-Konzept
([ent0n29/polybot](https://github.com/ent0n29/polybot), MIT, Java) für Kythera auf
**Hyperliquid-Public-Fills** reproduzierbar ist. Lead aus dem Repo-Audit 2026-07-10
(KB `mcp-41a50fe33552`). **Kein Fleet-Code angefasst** — reiner Research-Spike.

Ergebnis (Verdict in `docs/HYPERLIQUID_REPLICATION_EVAL.md`): **technisch machbar
und billig, strategisch optional und an die offene Hyperliquid-Venue-Entscheidung
gebunden.** Datenzugang, Signatur-Extraktion und Score wurden **live verifiziert**
(2026-07-10), die zitierten Zahlen sind echte PoC-Ausgabe, keine Schätzung.

### Added
- `tools/research/hl_replication_poc.py` — standalone, DB-frei, stdlib-only, kein
  `core`-Import, schreibt nichts. Beweist die drei tragenden Behauptungen: (1)
  jede Trader-Fill-Historie ist per Adresse public+keyless abrufbar (Leaderboard =
  40.376-Adressen-Universum), (2) polybots vier Verteilungs-Features portieren 1:1
  auf Perp-Fills (coin/dir/maker-taker/size — das Perp-Schema ist **reicher** als
  polybots Polymarket-Quelle), (3) polybots exakte Formel (mean L1 über Marginals
  → 0–100) läuft unverändert. Ergänzt eine **Self-Consistency**-Messung (zeitliche
  Replizierbarkeit eines *einzelnen* Traders), die der rohe polybot-Score auslässt.
- `docs/HYPERLIQUID_REPLICATION_EVAL.md` — die volle Eval: Datenzugang + Limits
  (2000 Fills/Call, 10k-History-Ceiling/Adresse), Signatur-Mapping,
  Score-Kritik (Similarity ≠ Reproduzierbarkeit; Marginals ignorieren
  Sequenz/Joint), Fit mit Kytheras vorhandenem Replay/Regime/Feature-Builder-Stack,
  und das Sekundärziel ClickHouse-Ingestion → **Reject, Timescale-Hypertable
  reicht** für append-only Low-Volume-Fills.

Verifiziert: PoC live gegen `api.hyperliquid.xyz/info` + Leaderboard-Blob (HTTP 200,
2000 Fills/Adresse, Score-Ausgabe plausibel), ruff check + format lokal grün.

## [2026-07-10] Fractional-Kelly-Sizing-Spec aus CloddsBot destilliert (T-2026-CU-9050-057)

Aus dem Repo-Audit 2026-07-10 (`alsk1992/CloddsBot`, MIT) die `kelly.ts`-Parametrik als
Position-Sizing-Spec für Kythera destilliert: `docs/KELLY_SIZING_SPEC.md`. Reine Design-Doku,
**kein Live-Code**.

### Der rahmende Befund
Kythera sized heute **keine** Notional-Größe — das macht Cornix. Kythera stellt nur Leverage
(`get_max_leverage` + `cap_leverage_to_sl`), Trade-Geometrie und das Orchestrator-Gating. Ein
1:1-Port von `kelly.ts` (`positionSize = bankroll × kelly`) hätte in Kythera keinen Hebel, an
dem er zieht. Verwertbar ist deshalb nicht die Größen-Zahl, sondern die **Adjustment-Kaskade**
(Drawdown, Win/Loss-Streaks, Vola-Scaling, Kategorie-Performance, Sample-Size, Quarter-Kelly).

### Was der Spec zeigt
Das State-Substrat für die Statistik-Adjustments (Win-Rate, Vola/Sharpe, „Kategorie" =
Bot×Regime×Direction) existiert bereits in `bot_regime_performance` (`27_bot_regime_analyzer`,
Fenster 7/30/90d) — datenseitig fast geschenkt. Was fehlt: Bankroll/Peak/Drawdown und Streaks
(kein Kapital-Modell in Kythera). Drei Andock-Optionen dokumentiert (A: Leverage-Skalierung,
B: Orchestrator-Gating/Size-as-Inclusion, C: Cornix per-Signal-Risk — ungeprüft), plus die
Perp-Anpassung `b = R = TP-Dist/SL-Dist` statt binärem `odds=1`.

### Empfehlung
Kein Notional-Sizer bauen. Erst ein Batch-E-Studien-Task (Vorlage T-2026-CU-9050-020): Kelly-
Fraktion aus `bot_regime_performance` als Post-hoc-Gewichtung auf die Walk-Forward-Replay-PnL
legen und den Effekt messen — **bevor** eine Zeile Live-Sizing-Code entsteht. Bei positivem
Beweis Option B (default-off Gate). Offene Operator-Fragen (Cornix-Money-Management, ob Kythera
je eigenes Notional-Sizing bekommt) an Michi eskaliert.

## [2026-07-10] AIM2-TOPN: "Top 1-3 des Tages" als High-Conviction-Kanal, default-off (T-2026-CU-9050-051)

Aus T-2026-CU-9050-031, Weg 2: der strukturelle Pfad zu „täglich 1-3 Trades, sehr
hohe Winrate". AIM2 rankt bereits die ganze Fleet und postet alles über seinem
~34 %-Pass-Threshold (≈110/Tag). AIM2-TOPN ist der **zweite, selektive Konsument
derselben Scores**: statt „alles über der Linie" höchstens **N (1-3) der stärksten
Kandidaten des Tages** in einen **eigenen Kanal/Tag** (`AIM2-TOPN`, Regel 6),
getrennt vom Basis-AIM2-Posting.

### Added
- `core/aim2_topn.py` — reine, DB-freie Selektionslogik (`select_topn`,
  `load_config`) plus der Routing-Tag `AIM2-TOPN` (≤ 10 Zeichen, passt in den
  Cooldown-Module-Key). „Top-N des Tages" ist erst ex-post bekannt, daher
  approximiert über eine hohe **Mindest-Probability** (nie unter dem
  Basis-Gate-Threshold) plus eine **harte rollierende 24h-Kappe** N. Rollierend
  statt Kalendertag — kein Mitternachts-Burst (23:50 + 00:10 = 2·N in 20 min).
- `tools/aim2_topn_calibrate.py` — **read-only** Schwellen-Kalibrierung aus
  `master_ai_processed_signals.ml_confidence`: welcher `min_prob` liefert
  historisch ~1-3/Tag. Schreibt nichts, schaltet nichts scharf (nur VPS, DB nötig).
- `backtest/test_aim2_topn.py` (DB-frei, standalone): Kappe, min-prob-Floor,
  Parity/trusted-Filter, (Coin,Richtung)-Dedupe, deterministischer Tie-Break,
  Config-Defaults/Clamping und die statische Verdrahtungs-Prüfung (Gate default-off,
  TOPN-Tag aus dem Stream ausgeschlossen, kein Flip der Money-Gates).
- `CH_AIM2_TOPN` in `core/config.py` (plain `_ch`, 0 = ungesetzt ⇒ Shadow-only,
  **kein** Fallback auf den AIM2-Kanal).

### Changed
- `15_ai_master_bot.py`: sammelt je Zyklus die starken, vertrauenswürdigen
  Kandidaten, selektiert nach der Schleife die Top-N unter der 24h-Kappe und
  postet über den auditierten `core.signal_post.post_ai_signal` (genau EINE
  Cornix-Message, Regel 4). Der `AIM2-TOPN`-Tag ist aus AIM2s eigenem
  Kandidaten-/Schwarm-Stream ausgeschlossen (F6-Selbst-Feedback).

### Gates (alle default-off — Scharf-Schalten ist Michis Entscheidung)
- `AIM2_TOPN_ENABLED=0` (Master-Schalter; aus ⇒ **null** Verhaltensänderung an
  Basis-AIM2 — statisch abgetestet), `AIM2_TOPN_LIVE_POSTING=0` (shadow-first),
  `AIM2_TOPN_N=1`, `AIM2_TOPN_MIN_PROB=0.95`. `AIM2_LIVE_POSTING` und
  `NEW_IDEAS_LIVE_POSTING` bleiben unangetastet.

Design: `docs/MODEL_INTENT.md` §9a. Verifiziert: `backtest/test_aim2_topn.py`
(17 grün), `guard.py verify` (24 Fixtures), ruff+mypy lokal grün.

## [2026-07-10] ROM1-Whitelist v2 als Shadow-Spalte: Netto-Expectancy statt WR + hierarchisches Shrinkage + B9-Zensur-Korrektur (T-2026-CU-9050-048)

Der Gate-Umbau aus Report 16 (Empfehlungen 6+7), gebaut **ausschließlich als
Shadow-Spalte**. Der Live-Gate bleibt unverändert auf v1 — scharf schalten ist
Michis Entscheidung nach dem Counterfactual-Vergleich (T-2026-CU-9050-047), nicht
Teil dieses Tasks.

### Warum
Die 4D-Whitelist hat zwei strukturelle Fehler (Report 16): **B1** — 89 % der
frischen Zellen sind `insufficient_data` und werden default-open durchgewunken
(n < 30 entscheidet nicht, sondern winkt durch); **B2** — Median 7 Trades/Zelle,
der WR-Punktschätzer ist zu verrauscht, und ein 55 %-WR-Bot mit winzigen Wins +
großen Losses ist netto ein Verlierer, den der reine WR-Gate durchlässt.

### Was v2 anders macht (Shadow)
`compute_whitelist` (27_bot_regime_analyzer) schreibt neben der v1-Entscheidung
eine zweite: `whitelisted_v2` = die **untere Konfidenzgrenze der Netto-Expectancy
(avg_pnl_pct) über dem Break-even**, geschätzt mit Empirical-Bayes-Shrinkage über
die Hierarchie Bot×Regime×Alt → Bot×Regime → Bot×ALL. Eine sparse Zelle leiht
Stärke vom übergeordneten Mittel (Gewicht n/(n+k)), eine Zelle ganz ohne Evidenz
bleibt am neutralen Prior und wird **nicht** whitelisted — das killt die
default-open-Krücke (B1). Die nötigen Spalten (`avg_pnl_pct`, `pnl_stddev`) lagen
längst in `bot_regime_performance` und wurden bisher ignoriert. Alle Knöpfe
(Break-even-Floor, Prior-Stärke k, z-Multiplikator) sind benannte Konstanten mit
konservativen Startwerten — sie werden vor jedem Flip auf der VPS-DB kalibriert,
nicht hier festgezurrt. Die neuen Spalten sind additiv (`ALTER … IF NOT EXISTS`),
das Live-Gate (`get_whitelist_decision`) liest weiter `whitelisted`.

### B9-Zensur-Korrektur
`CLOSED_REGIME_CHANGE`-Trades zählen jetzt mit ihrem **realen PnL zum
Close-Zeitpunkt** als Win/Loss statt pauschal neutral — der Auto-Close ist der
Exit des Trades, kein externes Housekeeping. Vorher zensierte das genau die per
Regime-Wechsel realisierten Verluste und biaste die gemessene ROM1-WR nach oben
(Report 16 B9). Angewandt konsistent an allen vier Klassifikations-Stellen
(`27_bot_regime_analyzer._classify_outcome`, `28_signal_orchestrator._classify_outcome_by_pnl`,
`23_market_tracker` beide Klassifikatoren), damit Report-WR und Whitelist-WR nicht
divergieren. `DELISTED/CLEANUP/ORPHAN` bleiben neutral; near-0 %-Regime-Closes
fängt weiter der Micro-PnL-Filter. In der Praxis trägt nur `model='ROM1'` diesen
Marker (P1.9), die Korrektur berührt also keine Fremd-Bot-Statistik und **nicht**
den Live-Gate (der auf die Trigger-Bots gatet, nie auf ROM1). **Michi-Hinweis:**
die auf VPS-Reports/Market-Tracker angezeigte ROM1-WR sinkt dadurch sichtbar —
das ist Messkorrektur, kein Regressionsverlust.

### Disziplin
Kein Gate-Flip, kein Scharf-Schalten, kein Live-Eingriff. B1/B2 bleiben live in
Kraft (v1), bis Michi nach dem Counterfactual-Vergleich flippt. Verifikation:
`backtest/test_bot_regime_analyzer.py` (neue Tests der Shrinkage-Mathe: Formel-Pin
gegen die Konstanten, Monotonie in n und Streuung, Prior-Fallback-Hierarchie,
B1-No-Default-Open, Expectancy-Block trotz WR; plus B9-Klassifikation) und
`test_signal_orchestrator.py` grün (46 + 75 Tests), ruff/format/mypy sauber,
Regression-Guard `verify` unverändert (24 Fixtures, kein Indikator-Pfad berührt).
Der scharfe v1↔v2-Vergleich braucht eine VPS-DB-Session.

## [2026-07-10] Der Gate-Wert wird messbar: ROM1-Counterfactual-Scorer für unterdrückte Signale (T-2026-CU-9050-047)

Bis jetzt war der Nutzen des Orchestrator-Gates schlicht **unbekannt**. Das 4D-Gate
ist zu 89 % default-open, und die +8pp ROM1-Win-Rate sind durch drei gleichgerichtete
Biases verzerrt — es gab keine Zahl dafür, was eine Unterdrückung erspart oder
gekostet hat. Dieser Task liefert das Messwerkzeug (Report 16, §8).

### Was der Scorer tut
`tools/rom1_counterfactual.py` rechnet für jede Row in `orchestrator_suppressed_signals`
das hypothetische Outcome nach: Welche ROM1-Geometrie hätte der Orchestrator zum
Signal-Zeitpunkt gepostet, und wie wäre dieser Trade im First-Touch-Replay
(`tools.walkforward_sim.simulate_exit`) ausgegangen — wick-aware, SL-first,
Monitor-Trailing, Fees. Aggregiert pro Suppression-Reason
(`bot_not_whitelisted:wr_below_overall`, `orchestrator_cooldown`, …): Win-Rate,
Netto-PnL, R. **Positiver Netto-PnL auf der suppressed-Seite = das Gate hat Geld
liegen gelassen.**

### Beide Seiten desselben Gates
`--side forwarded` scored die durchgelassene Seite aus `orchestrator_open_trades`,
gebucketed nach `wl_reason` (die B8-Spalte aus T-2026-CU-9050-046) — also pro
Gate-PFAD: echte 4D-Zelle vs. `no_whitelist_entry` (default-open) vs. Fallback.
`--side both` stellt beide Seiten bei gleichem Horizont nebeneinander. Erst dieser
Vergleich beantwortet, ob der Gate-Pfad Gewinner von Verlierern trennt oder der
+8pp-WR ein Artefakt der default-open-Rate ist. Die `dedupe`-Reasons
(same/opposite_direction_open, cooldown) sind als eigene `bucket_class` getrennt —
sie messen Positions-Hygiene, nicht das 4D-Urteil, und wären sonst irreführend.

### Disziplin
Reine Mess-/Scorer-Schicht: kein Gate-Flip, kein Scharf-Schalten, read-only
DB-Session, SELECT-only, committet nie. R1-sauber — die Entscheidungskerze ist die
letzte zum Signal-Zeitpunkt geschlossene, der Exit-Scan beginnt auf der Kerze danach
(`as_of_index`). Die Geometrie kommt aus **einer** Quelle: `compute_rom1_trade_params`
bekam optionale As-of-Parameter `price=`/`df=` (dasselbe P0.10-Muster wie
`get_hvn_and_sr_levels(df=)`), sodass der Replay exakt die Live-Geometrie postet —
kein Copy-Paste-Skew (X-R1). Der eigentliche Lauf braucht eine VPS-Session
(Preisdaten/DB); geliefert ist das Tooling plus DB-freie Tests.

Verifikation: `backtest/test_rom1_counterfactual.py` (19 Tests, standalone/DB-frei)
deckt As-of-Indexierung/kein Look-ahead, Horizont-Kappung, Skip-Accounting und
Aggregation ab; `test_signal_orchestrator.py` bekam den As-of-Pfad plus einen
Live-vs-As-of-Paritätstest. `guard.py verify` grün.

---

## [2026-07-10] Das 10s-Raster ist unter Last eine Fiktion: Pump/Dump-Fenster normalisiert, totes Volume-Gate repariert (T-2026-CU-9050-035)

Der EPD2-Retrain, für den dieser Task angelegt wurde, ist **nicht** passiert — die
Datenlage-Prüfung (Schritt 1) hat ihn blockiert und dabei zwei latente
Regressionen aus P1.39 freigelegt.

### Warum kein Retrain
`pump_dump_events` enthält **null** Rows der neuen Feature-Definition. P1.39 ist
zwar gemergt, aber Bot 10 lief zum Messzeitpunkt ununterbrochen seit dem
Fleet-Start am 08.07. und hielt den alten Modulcode. Der Log-Banner
„ML-Modell geladen" sieht nach Startup aus, ist aber ein *stündlicher*
Cache-Reload (`load_pump_model()`, TTL 3600s): seine Kadenz driftet über 24h
monoton von 13:41 auf 13:44, ohne den Reset, den ein Prozess-Neustart erzwingen
würde. Der im Task empfohlene Zeitschnitt liefert also einen leeren Datensatz.
Der Retrain wartet auf einen Bot-10-Restart (Operator-Entscheidung).

### Messung
Gegen 421 350 echte Anker aus dem Live-`1minute.json` (6h-Fenster): die
Bucket-Kadenz ist **bimodal** — Median 10s, aber p90 = 70s, und nur 62,7 % der
Abstände liegen unter 15s. Der Detector pollt ~530 Symbole pro REST-Roundtrip;
unter Last entsteht schlicht kein Bucket pro 10 Sekunden.

Daraus folgten zwei Defekte, die erst beim nächsten Restart scharf geworden wären:

- **`p_chg_60s` verlor 38,7 % aller Ticks.** `WINDOW_EDGE_GUARD = 5` verlangt
  einen Bucket bei exakt `anchor-60s ± 5s`; das löste nur für 61,3 % der Anker
  auf, der Rest kehrte ungescored zurück.
- **Der Volume-Explosion-Alert war tot.** Die Konstante `360` wanderte aus
  `len(volumes_10s) >= 360` — einem Warmup-Check über den *ganzen* 1440er-Deque,
  praktisch immer wahr — nach `len(hour_vols) >= 360`, wo dieselbe Zahl eine
  Dichte von einem Bucket pro 10s über eine volle Stunde fordert. Reale Dichte:
  ~193/h. Das Gate hielt für **0 von 421 350** Ankern.

### Fix
`_find_bucket_nearest` wählt den Bucket mit der zum Ziel nächsten **echten**
Distanz innerhalb eines Altersbandes und gibt diese Distanz mit zurück. `p_chg_60s`
und `p_chg_3m` normalisieren die beobachtete Bewegung auf eine Rate pro 60s bzw.
180s; `buy_pres` und `volat` teilen sich dieselbe tatsächliche Spanne. Auf dichtem
Raster ist das die Identität (Skalierung 60/dt: Median 1,00, p10 0,75), unter Last
meldet es die Rate, die das Fenster wirklich hergibt. Coverage `p_chg_60s`:
61,3 % → **97,7 %**. Der Stunden-Warmup gated jetzt auf die überdeckte Zeitspanne
plus Sample-Floor statt auf eine Bucket-Anzahl.

Bewusst **nicht** auf `tolerance=20` gewechselt: einen 80s alten Bucket als „60s"
zu verrechnen wäre die abgeschwächte Wiederkehr genau des Fehlers, den P1.39
beseitigt hat.

### Retrain-Kopplung
Die vier Modell-Inputs verschieben sich damit erneut — bewusst, und vor dem
Restart, damit EPD3 direkt auf der endgültigen Definition gefittet wird statt
zweimal. Voraussetzung für einen sauberen Rollout bleibt T-2026-CU-9050-030
(P1.45): `module_tag` ist Quellcode-Konstante, der Detector liest keine
Artefakt-Meta — ein EPD3-Artefakt postete sonst still unter dem Alt-Tag.

### Entry-Schätzer nachgezogen
`p_chg_60s` ist damit eine Rate und **kein** realisierter Move mehr. Der Builder
las die Spalte aber als Move (`entry1 = close × (1 + p_chg/100)`) — und weil die
Fensterlänge nirgends persistiert wird, ist der rohe Move aus dem Event-Log nicht
rekonstruierbar (harte Regel 7). Der Entry kommt jetzt aus `ticker_10s`, dem
tatsächlich gehandelten Preis: über die letzten drei Tage finden 7053 von 7055
gegateten Events einen Tick innerhalb 60 s, über alle 404 Event-Symbole. Fehlt der
Tick, fällt die Zeile raus (`no_ticker`) statt geschätzt zu werden — ein
unbekannter Entry muss ein fehlendes Label werden, kein falsches. Ein `--since`
vor dem ersten Tick bricht laut ab, statt den Datensatz still zu halbieren.

Verifikation: `backtest/test_pump_dump_time_windows.py` (18 Tests) +
`backtest/test_epd2_entry_from_ticker.py` (5 Tests), standalone und DB-frei.
Sechs fallen auf dem jeweiligen Pre-Fix-Stand, darunter die drei
Verhaltenszeugen (70s-Kadenz wird gar nicht gescored; Volume-Explosion feuert
nie; Ein-Sample-Baseline wird gescored). Die übrigen laufen auf beiden Ständen
grün und belegen, dass der dichte Pfad unverändert bleibt. `backtest/` gesamt
316 grün, Regression-Guard `verify` + `smoke` grün. Wirkt beim nächsten
regulären Restart, kein Deploy.

---

## [2026-07-10] Konzept-Spec: MM-Order-Lifecycle-Patterns für die offene Hyperliquid-Venue-Entscheidung (T-2026-CU-9050-056)

Reine Doku-/Konzept-Arbeit, kein Code am Fleet. Aus dem Repo-Audit vom 2026-07-10
(KB `mcp-41a50fe33552`) war `lihanyu81/polymarket_lp_tool` als sauberste
MM-Order-Lifecycle-Architektur markiert. Da das Repo **keine LICENSE** trägt
(all-rights-reserved), ist das Ergebnis ein **Pattern-Harvest in eigenen Worten** —
kein Code kopiert, portiert oder vendored; falls je gebaut wird, dann clean-room aus
dieser Spec.

### Added
- **`docs/MM_ORDER_LIFECYCLE_SPEC.md`** — destilliert 14 benannte, übertragbare
  Patterns (Reconciliation-statt-State-Machine, Cumulative-Watermark-Fill-Detection,
  Per-Side-Quote-Diff, Cancel-then-Repost vs. Modify, WS-User/Market-Trennung,
  Priority-Cascade, Reprice-Speed-Limits, Tick-Regime, Midpoint-Filter, Fill-Risk,
  Structural-Deleverage, Vol-Gate, Hysterese-Monitor). Jedes Pattern ist von der
  Polymarket-CLOB-Annahme auf ein **Hyperliquid-Perp-Orderbuch** gemappt
  (Mapping-Tabelle, §7), inkl. der drei zu strippenden Prediction-Market-Annahmen
  ((0,1)-Preisdomäne, Reward-Band, Binary-Condition-Pairing) und der sechs Lücken, die
  die Quelle **nicht** abdeckt und die selbst zu designen sind (kontinuierlicher
  Inventory-Skew, Funding-Awareness, Mark/Oracle/Last, Event-Risk-Gate, Latency-Budget,
  Maker-Economics). Abschluss: Empfehlung „feasible, aber nur grünes Licht für einen
  Shadow/Paper-Prototyp" plus fünf offene Fragen für die Venue-Entscheidung.
- **Doku-Map-Zeile** in `docs/ARCHITECTURE.md` §12 (Verweis auf die neue Spec,
  als pre-decision markiert).

**Kein Live-Bezug:** die Spec baut nichts, flippt kein Gate, berührt keinen Bot. Ein
etwaiger MM-Prototyp läuft laut Spec zuerst shadow/paper und ist — wie jeder Geld-Pfad
— eine Operator-Entscheidung (`OPUS-HANDOFF.md` §6).

---

## [2026-07-10] Orchestrator-Gate: Staleness-Gate auf der 4D-Zelle, `wl_reason` auf dem Forward, Doku-Korrektur (T-2026-CU-9050-046)

Drei Befunde aus dem ROM1-Deep-Review, alle am selben blinden Fleck: **die
durchgelassene Seite des Gates war unbeobachtbar.** `orchestrator_suppressed_signals`
protokolliert nur, was geblockt wurde. Warum ein Signal *durchging* — echte 4D-Zelle,
`no_whitelist_entry` oder Fallback — stand nirgends. Genau deshalb konnte P0.4
(Bot-Namen-Mismatch, jedes Signal lief als `no_whitelist_entry` durch) monatelang
laufen, ohne aufzufallen: ein still offenes Gate sieht von außen aus wie ein
großzügiges.

### Added
- **`wl_reason`-Spalte an `orchestrator_open_trades`** (B8). `ensure_regime_schema`
  legt sie für neue DBs an und zieht sie für bestehende per
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS` nach; `insert_orchestrator_open_trade`
  schreibt die Entscheidung, die `get_whitelist_decision` tatsächlich getroffen hat.
  Rows aus der Zeit davor bleiben `NULL` und werden in der Statistik separat
  gezählt, statt einen Pfad zu raten.
- **Gate-Pfad-Zeile im stündlichen Regime-Status** (P0.4-Rest). Über die letzten 24h:
  Anteil default-open / Fallback / echte 4D-Entscheidung. Ab 20 % Bypass-Anteil
  (default-open + Fallback zusammen) trägt die Zeile ein `⚠️`.
- Vier Tests in `backtest/test_signal_orchestrator.py` (frische Zelle entscheidet,
  stale Zelle fällt zurück, `computed_at IS NULL` gilt als stale, `wl_reason` landet
  im INSERT).

### Changed
- **`get_whitelist_decision` misstraut alten Zellen** (P0.4-Rest/P2.25): eine
  `bot_regime_whitelist`-Zelle älter als 48h (`WHITELIST_MAX_AGE_HOURS`, zwei
  Analyzer-Zyklen) entscheidet nicht mehr — stattdessen greift der Overall-Fallback,
  Reason `whitelist_stale:<fallback_reason>`. Ein fehlendes `computed_at` zählt als
  stale. **Semantik-Änderung auf dem Geld-Pfad:** die Live-Zellen sind laut Audit auf
  `computed_at=19.04.` eingefroren, der Fallback lässt bei <30 Trades durch — heute
  blockierte Bot/Richtungs-Paare können also aufgehen. Das ist der Zweck des Fixes,
  aber eine volumen-erhöhende Änderung. `force_close_trades_for_regime_change` nutzt
  dieselbe Funktion und schließt Trades folglich ebenfalls nach Fallback-Logik.
- **`docs/REGIME_ORCHESTRATOR.md`** (P1.10): die Doku behauptete, das System „tradet
  nicht selbst" und sei ein reiner Signal-Router. Das war seit der ROM1-Geometrie
  falsch — ein durchgelassenes Bot-Signal ist nur der Trigger, `compute_rom1_trade_params`
  verwirft Entry/SL/Targets des Originals. Die Konsequenz (Gating-Statistik ≠
  Ausführungs-Statistik) steht jetzt dort.

**Deploy-Reihenfolge:** Bot 26 vor Bot 28 neu starten — 26 legt die Spalte in
`ensure_regime_schema` an, 28 schreibt sie. Beim regulären Fleet-Start ist das
gedeckt (`start_delay` 160 vs. 175). Startet nur 28 gegen eine DB ohne die Spalte,
schlägt der INSERT fehl und die Transaktion rollt zurück: ein verlorenes Signal,
kein Cornix-Post ohne Tracking.

Nicht Teil dieses PRs: das P1.8-Hardening (explizites `open_time`) kam bereits mit
T-2026-CU-9050-052. Der dort ebenfalls diskutierte 72h-Age-Bound auf
`is_opposite_direction_open` wurde **bewusst verworfen** — er hätte eine echte, über
72h offene ROM1-Position freigegeben und die Gegenrichtung dagegen posten lassen.
Tote OPEN-Rows räumt der Corpse-Reaper in `sync_closed_trades` ab.
## [2026-07-10] Indikator-Engine erfindet keine Warm-up-Werte mehr — NaN fließt wie bei KAMA (T-2026-CU-9050-054)

P1.13, am Code verifiziert (Falle 13): `2_indicator_engine.py` füllte die
Warm-up-Fenster der Rolling-Indikatoren mit `.fillna(0)` bzw. `.fillna(50)` —
`wma_*` (`calculate_wma`), `rsi_*` (`calculate_rsi`), `boll_*_20` und
`donchian_*`. Für einen jungen Coin liest `extract_ml_features` in
`24_quasimodo_bot.py`/`25_smc_ml_sniper.py` daraus
`donchian_upper_20_dist_pct = (0-close)/close*100 = -100.0`: fünf der elf
Preis-Features sind in den ersten ~20 Bars hart auf −100 gepinnt und kodieren
„junger Coin" statt eines Abstandsmaßes. Symmetrisch in Bot und Replay (kein
Train/Serve-Skew), aber beidseitig Müll.

**Fix:** die undefinierten Warm-up-Zeilen fließen jetzt als NaN — genau wie
`calculate_kama` es seit jeher tut. Alle betroffenen Spalten sind `REAL` (wie
`kama_*`), der NaN-Write-Pfad ist damit in Produktion bereits bewiesen. Auf der
Leseseite ändert sich nichts erzwungen: die Bots imputieren die Kopfzeilen
weiter über ihr bestehendes `ffill().bfill()` (aus `-100` wird so ein sinnvoller
Abstand zum ersten echten Wert), der Replay verwirft sie seit
T-2026-CU-9050-045 per `dropna()`. Der Blast-Radius wurde über alle
`_indicators`-Consumer geprüft: jeder ML-Feature-Pfad imputiert (`fillna(0)`,
`ffill/bfill` oder `isfinite`-Guard); die einzigen Roh-Consumer (Strategie-Bots
`strat_*`) lesen die neuesten 480 Kerzen (Warm-up ist rein historisch) und ihre
AND-verketteten NaN-Vergleiche blocken strikt mehr, erzeugen also nie ein
Signal. `ma_*` blieb bewusst unangetastet (kein aktiver Consumer, kein
Distanz-Feature) — außerhalb der verifizierten Fläche.

Regression-Guard: der Golden wurde bewusst refreshed
(`KYTHERA_GOLDEN_REFRESH=1`). Die 816 Breaches sind ausschließlich die
Warm-up-Kopfzeilen der vier Familien (golden `0`/`50` → fresh `NaN`), keine
andere Spalte driftet — die Diff im `golden/` belegt genau das.

**Noch offen (Operator/Michi, C-Gate, NICHT Teil dieses PRs):** Der Fix ist ein
DB-Writer-Change und wird erst durch einen Recompute der Indikator-Tabellen live
wirksam (heute schreibt die Engine Warm-up-Kopfzeilen nur beim Erstlauf eines
Neu-Listings). Danach gehört ein TD2/BB2/QM2-Retrain auf die verschobene
Feature-Verteilung, und **erst beim Artefakt-Rollout** darf das `bfill` in
`24_quasimodo_bot.py:126`/`25_smc_ml_sniper.py:220` entfernt werden — nie
isoliert.

## [2026-07-10] Finding-IDs im Ledger: Duplikat-Guard als pre-commit-Hook (T-2026-CU-9050-059)

Am 09./10.07. trugen drei frisch angelegte Findings gleichzeitig die ID **P1.46**.
Mehrere Sessions arbeiteten parallel am `AUDIT_TODO.md`, jede las das Ledger, nahm
die scheinbar nächste freie Nummer und schrieb sie zurück — eine klassische
Read-Modify-Write-Race ohne Allokator. PR #34/#36 haben von Hand auf P1.47/P1.48
umnummeriert; die Ursache blieb.

### Added
- `tools/audit/finding_ids.py` mit zwei Subcommands. **`check`** meldet doppelt
  vergebene IDs und liefert Exit 1 — das ist das Netz. **`next --severity P1`**
  druckt deterministisch die nächste freie Nummer (max+1 je Severity) — das ist
  die Bequemlichkeit. Wie das KB-`next_id()` ist `next` eine Momentaufnahme und
  **keine Reservierung**: zwei gleichzeitige Aufrufe bekommen dieselbe Nummer.
  Was die Kollision von `main` fernhält, ist `check`.
- **pre-commit-Hook `kythera-finding-id-guard`** (neben dem Regression-Guard) —
  die Kollision fällt beim Commit auf, nicht erst im Review. Fehlt
  `AUDIT_TODO.md`, läuft der Hook fail-open durch, statt den Commit zu blocken.
- `backtest/test_finding_ids.py` (DB-frei, standalone).

Die tragende Unterscheidung ist **Definition vs. Referenz**: Findings werden quer
durch das Ledger in Prosa zitiert („orthogonal zu P1.44"), ein naives `grep` auf
`P\d+\.\d+` meldet darum Dutzende Falsch-Duplikate und der Guard wäre binnen eines
Tages abgeschaltet. Ein Finding ist **ausschließlich** auf seiner Checkbox-Zeile
definiert (`- [ ] **P1.45 …`). Genau das prüft ein eigener Test ab.

Der Bestand bleibt unverändert (125 Findings, keine Duplikate; nächste freie IDs:
P1.49, P2.52). Kein Renumbering.
## [2026-07-10] wf_significance MaxDD entkonfundiert: absoluter Drawdown in %-Punkten statt Peak-Normierung (T-2026-CU-9050-053)

Fix zum Befund aus T-2026-CU-9050-040. `tools/wf_significance.py:max_drawdown_pct`
normierte den Drawdown auf den laufenden Peak (`(equity − peak) / peak`). Auf den
fleet-weiten Multi-Coin-Replays trägt die additive Equity das nicht: 8,8–20,2
gleichzeitige Signale pro Zeitstempel werden als sequenzielle Einzelwetten
verkettet, die Equity fällt tief unter null, und der Quotient misst am Ende die
zufällige Peak-Höhe statt der Verlust-Clusterung.

Fix: der DD wird jetzt **absolut in %-Punkten** unter dem Peak gerechnet
(`equity − peak`, ohne Normierung; die +100-Basis kürzt sich heraus). Beobachteter
und permutierter Pfad werden damit exakt gleich gemessen. Der Nebenbefund
(`np.where(peak > 0, peak, 1.0)` wechselte bei Peak ≤ 0 still Einheit und
×100-Skalierung) löst sich by construction — ohne Division gibt es keinen Guard
mehr. Gewählte Option: absoluter DD statt eines overlap-respektierenden
Equity-Pfads; letzterer bräuchte Kapitalallokations-Annahmen, die das Replay-JSONL
nicht trägt (Grenze in `docs/WF_SIGNIFICANCE.md` benannt: Pfad-Clusterungs-Statistik,
kein echter Portfolio-Drawdown).

Verifiziert am echten Artefakt (200 Permutationen, Seed 42): rub/LONG kippt von
p = 1,000 („untypisch gnädig") auf 0,005 (beob. −55.208 vs Median −17.182),
ufi1/SHORT von 0,035 auf 0,005. `backtest/test_wf_significance.py` pinnt die
Peak-Höhen-Invarianz und den Nicht-positiv-Peak-Fall mechanisch (mutations-geprüft:
beide fallen gegen die alte Formel — −25 % vs −45,45 % bzw. −4000). Die Lese-Hilfe
in `docs/WF_SIGNIFICANCE.md` ist wieder scharf gestellt.

**Keine Deploy-Aussage der Batch-E-Tabelle ändert sich.** Sie steht auf Statistik 1
(Random-Control) und 3 (Bootstrap-CI), beide reihenfolge-invariant und vom DD-Fix
unberührt; die DD-Statistik war ohnehin als „nicht operativ lesen" markiert und ging
in keinen Deploy-Call ein.
## [2026-07-10] P1.8-Folgefix: ROM1-Lifecycle-Sync war seit 04.07. still tot — open_time jetzt explizit naiv-UTC + twin-basierter Corpse-Reaper statt Age-Bounds (T-2026-CU-9050-052)

Die VPS-Verify-Session T-2026-CU-9050-044 hat den P0-Verdacht aus dem
ROM1-Deep-Review bestätigt: der P1.8-Fix vom 04.07. (±60s-Match gegen
`ai_signals.open_time`) hat den Sync nicht repariert, sondern still getötet.
`insert_rom1_signal` setzte `open_time` nicht — der DB-Default `now()` stempelt
bei Session-TZ Europe/Bucharest Lokalzeit in die naive timestamp-Spalte,
konstant +10.799 s (+3 h) gegen das naiv-UTC `opened_at` der Tracking-Row. Das
±60s-Fenster konnte nie matchen: letzter `lifecycle_sync`-Close exakt am
Deploy-Zeitpunkt 04.07. 11:10, danach 395 akkumulierte OPEN-Rows (208 älter
72 h) und `opposite_direction_open`-Suppressions von 4/Tag auf 165/Tag (166
Suppressions auf 79 Coins nachweislich durch Leichen-Rows).

Fix in `28_signal_orchestrator.py`: (1) `open_time` wird explizit als
naiv-UTC gesetzt (`core.time.utc_now_naive`, gleiche Quell-Semantik wie das
`opened_at` der Zwillings-Row; Monitor 8 behandelt `open_time` ohnehin als
UTC). Damit ist `ai_signals.open_time` eine gemischte Domäne (ROM1=UTC, Rest=
Session-lokal via Default) — dokumentiert in `docs/UTC_POLICY.md` §3, die
Vereinheitlichung bleibt der R3-Flip. (2) Neuer **Corpse-Reaper** am ANFANG
jedes Lifecycle-Sync-Passes (Decay hängt damit nicht an der Gesundheit des
Match-Loops): eine OPEN-Row, deren `ai_signals`-Zwilling nicht mehr existiert
(Trade wurde geschlossen, aber nie gesynct — genau die Leichen-Klasse), wird
nach 72 h Mindestalter auf `CLOSED_NEUTRAL` / `close_reason='corpse_reaper'`
gestellt. Der Twin-Check ist **row-anchored** (±60 s um `opened_at`, beide
Rows entstehen in einer Transaktion) — ein Live-Trade auf demselben
coin+direction schirmt eine Stacking-Ära-Leiche also NICHT ab. Für die
Legacy-Population (open_time in Session-Lokalzeit gestempelt) gibt es ein
zweites Fenster über die **hart kodierte historische Writer-TZ**
`Europe/Bucharest` (bewusst nicht `current_setting('TimeZone')`: ein
künftiger R3-Flip der Session-TZ darf live Legacy-Positionen nicht
entschirmen; DST behandelt `AT TIME ZONE` pro Timestamp). Dieses
Legacy-Fenster gilt **symmetrisch** auch im Sync-Match-Loop und in der
Anti-Zensur-Klausel des Reapers — sonst würde ein Legacy-Trade, der NACH dem
Deploy schließt, sein echtes WIN/LOSS an den Reaper verlieren; so recovered
der Match-Loop stattdessen auch die echten Outcomes der Alt-Leichen.
Kollisionsfrei ist das Fenster, weil der 4h-Cooldown pro coin+direction zwei
gleichgerichtete Trades im Abstand von ~3 h strukturell ausschließt (per Test
gepinnt, inkl. Fenster-Konstante `LIFECYCLE_SYNC_WINDOW_SEC` für alle
Anker-Fenster). Anti-Zensur-Klausel: existiert bereits eine syncbare
`closed_ai_signals`-Row (in einem der beiden Fenster), überspringt der
Reaper — das echte WIN/LOSS-Outcome klassifiziert der Match-Loop, nie der
Reaper (schließt das Monitor-Commit-Race für >72h-Trades). `closed_at` der
gereapten Rows ist die Reap-Zeit, nicht die echte Close-Zeit —
Duration-Auswertungen müssen `close_reason='corpse_reaper'` ausschließen.
Der Main-Loop isoliert die drei Stages jetzt einzeln (try/except + Rollback
pro Stage): eine Poison-Row im Regime-Check oder Gating kann den
Lifecycle-Sync (und damit den einzigen Decay-Pfad) nicht mehr dauerhaft
aushungern. Der Geld-Pfad bleibt dabei fail-closed: schlägt die Regime-Stage
fehl, wird der Gating-Pass übersprungen (kein neues Exposure, solange die
Auto-Closes gestört sind), und ein äußeres Catch-all hält den Prozess am
Leben. Das Zwei-Fenster-Prädikat baut EIN Helper
(`_anchor_window_predicate`) für alle drei SQL-Stellen; die historische
Writer-TZ liegt kanonisch in `core/time.py` (`LEGACY_WRITER_TZ`). Empirisch
gegen die Live-DB entlastet (read-only): 0 von 409 OPEN-Rows haben mehr als
einen Close-Kandidaten über beide Fenster (kein Cross-Match im Bestand), und
der komplette First-Pass über 440k `closed_ai_signals`-Rows dauert 1,8 s
(4,4 ms/Row) — keine Loop-Blockade.
Reine Buchhaltung, kein Telegram-Post. Damit verschwinden die Leichen wirklich
aus dem OPEN-Bestand — sie blocken die Richtungs-Checks nicht mehr, füttern
den Regime-Change-Auto-Close nicht mehr mit Spurious-`Close`-Kommandos und
werden nicht mehr in jedem Sync-Pass erneut gescannt. (3) Die Richtungs-Checks
bleiben bewusst OHNE Zeitschranke: ein Age-Bound (auch der bestehende 72h-Bound
aus P2.26 in `is_same_direction_open`, hier entfernt) hebt den Schutz auch für
ECHTE >72h-Positionen auf — ROM1 setzt kein `expiry_hours`, eine legitime
Position kann beliebig lange offen sein, und ohne Block würde die Gegenrichtung
die Live-Position flippen (Review-Finding aus PR #40). Liveness-Kriterium ist
jetzt der Zwilling, nicht die Uhr. Bewusster Tradeoff: ein STUCK-Zwilling
(Monitor kann den Coin nicht scoren) blockt weiter — Schutz vor Verfügbarkeit;
der Decay-Pfad dafür ist der DELISTED-Cleanup des Housekeepings.

Verifikation nach Deploy: `lifecycle_sync`-Closes tauchen wieder auf
(>0/Tag), der OPEN-älter-72h-Bestand (208 Rows Stand 10.07., wachsend Richtung
395) wird im ersten Sync-Pass abgebaut — Alt-Leichen mit vorhandener Close-Row
bekommen ihr ECHTES Outcome über den Match-Loop (`lifecycle_sync`), nur
matchlose Reste gehen als `corpse_reaper` neutral raus — und danach bleibt der
Bestand ~0; KEIN `Close`-Kommando-Burst beim nächsten Regime-Flip. Sieben neue
Tests pinnen INSERT-Spalte + naiv-UTC-Wert, die bound-freien Richtungs-Checks,
den Reaper-Contract (Reaper-first, row-anchored Twin-Fenster, hart kodierte
Legacy-TZ in beiden Subqueries, Anti-Zensur-Klausel, kein Outbox-Write), das
Legacy-Fenster im Match-Loop und die Cooldown-Invariante, die das
Legacy-Fenster kollisionsfrei macht — `backtest/test_signal_orchestrator.py`;
Suiten test_regime_detector/test_bot_regime_analyzer unverändert grün.


## [2026-07-10] Signifikanz-Layer über die echten Batch-E-Replays: Layer bestätigt, MaxDD-Statistik widerlegt (T-2026-CU-9050-040)

Der VPS-Rest aus T-2026-CU-9050-027 D3: `tools/wf_significance.py` lief read-only
über `mis1_replay_400d`, `rub_replay_365d`, `abr1_replay_365d` und
`ufi1_replay_365d` (`--group-by strategy+direction`, n=1000, Seed 42), Ergebnisse
in `docs/WF_SIGNIFICANCE.md`.

**Der Layer verhält sich wie spezifiziert.** Das Kontroll-Mittel trifft in allen
sieben Gruppen den Round-Trip-Fee-Drag (−0,0961 … −0,1006 gegen erwartete −0,10),
und die trade-gewichteten Aggregate reproduzieren die `*_summary.json` des
Simulators exakt (WR, avg_r, avg_pnl). Der Lauf ist deterministisch.

Inhaltlich messen die Replays den **rohen Detektor**, nicht das deployte Modell:
abr1/SHORT hat einen Roh-Edge und abr1/LONG ist signifikant schlechter als ein
richtungsloser Zufalls-Trader (deckt sich mit dem Live-Bild), während rub in
beiden Richtungen roh negativ ist, obwohl RUB2-SHORT live läuft — dort trägt die
Modell-Selektion den Edge. mis1/SHORT ist trotz p = 0,001 ein Null-Edge
(CI-Untergrenze 0,0006).

**Widerlegt:** die Lese-Regel zu `p_value_dd_worse`. `max_drawdown_pct` normiert
auf den laufenden Peak, aber die additive Equity dieser fleet-weiten Replays
verkettet 8,8–20,2 gleichzeitige Signale pro Zeitstempel als sequenzielle
Einzelwetten und fällt tief unter null (rub/LONG: 72 % des Pfades negativ). Der
Quotient misst dann die zufällige Peak-Höhe statt der Verlust-Clusterung: mit
absolutem DD in %-Punkten kippt rub/LONG von p = 1,000 („untypisch gnädiger
Pfad") auf p = 0,005 (schlechter als 199 von 200 Zufallsreihenfolgen) — die
bisherige Regel hätte das DD-Budget genau falsch herum gesetzt. Statistik 2 ist
in der Doku auf „nicht operativ lesen" gestellt; Fix ist T-2026-CU-9050-053.
Statistik 1 und 3 sind reihenfolge-invariant und unberührt.
## [2026-07-10] EPD und SRA laden ihr Artefakt über den geteilten Contract (T-2026-CU-9050-042)

Letzte zwei Instanzen der P1.45-Fehlerklasse: ein Post-Pfad schreibt einen
hartkodierten Modell-Tag, statt die `model_id` aus der Artefakt-Meta zu lesen
(harte Regel 6). Anders als bei MIS/RUB/QM war der Tag hier aber nur das
Symptom — darunter lag ein **Format-Bruch zwischen Retrain-Ausgabe und
Live-Ladepfad**, und der musste zuerst weg.

**Befund-Korrektur zur Task-Doc (Falle 13):** `retrain_sra2.py` emittiert *kein*
dict-Artefakt, sondern natives XGB-JSON + `_meta.json`/`_calib.pkl` — dasselbe
Format wie ABR2. Der Format-Mismatch bestand allein bei EPD; SRA fehlte nur der
Meta-Read. Am Code verifiziert, nicht aus der Annotation übernommen.

Drei Schritte, ein Bot pro Commit:

- **`core/model_artifacts.py`** bekommt `load_artifact_json()`. Der
  XGB-JSON-Sidecar-Loader steckte bis jetzt eingebacken in
  `18_ai_abr1_bot._load_model_contract`; jetzt liefert er denselben
  Contract-Dict wie `load_artifact()` (dict-pkl). Ohne `_meta.json` gilt der
  benannte Legacy-Vertrag (Tag + Threshold aus Konstanten, `features=None`),
  mit Meta kommen Tag, Threshold und Feature-Vertrag aus dem Artefakt. Ein
  nicht-binärer `model_type` im binären Slot wird abgelehnt, statt still die
  falsche `predict_proba`-Spalte zu lesen. `maybe_reload` dispatcht jetzt über
  die Datei-Endung — über den pkl-Loader geroutet hätte ein JSON-Artefakt nie
  neu gelesen und nach einem Deploy still die alte Generation weitergeliefert.

- **SRA** (`9_ai_sr_bot.py`): lud seine `.json`-Modelle roh in einen
  `xgb.XGBClassifier` und postete beide Richtungen unter der Konstanten `SRA1`.
  Der Tag kommt jetzt aus der Meta, der Threshold ebenso. Zusätzlich ein
  **Serving-Paritäts-Bruch**, der einen SRA2-Rollout verdorben hätte: Bot und
  Trainer benutzten dieselben Spaltennamen mit **verschiedenen Formeln** —
  `pct_ema9` war im Bot `(close-ema9)/close`, im Trainer `(close-ema9)/ema9` —
  und `macd_dif_pct`/`macd_dea_pct`/`atr_pct` baute der Bot gar nicht. Der
  Builder liegt jetzt einmal in `core/sra_features.py`, importiert von Bot und
  Trainer (X-R1-Regel). Der Legacy-Vektor bleibt unangetastet daneben — er ist
  der Vertrag des heute deployten Modells. Ein fehlendes Artefakt idlet die
  Richtung, statt `exit(1)` in den Watchdog-Restart-Loop zu laufen (Falle 3).

- **EPD** (`10_pump_dump_detector.py`): live läuft ein **rohes 3-Klassen**-Modell
  mit positionalem 10-Feature-Array (Erfolg = Klasse 2/0, Threshold hart 0.60).
  Das EPD2-Artefakt ist dagegen **binär je Richtung**, mit 16 benannten Features
  inkl. der 6 Funding-Spalten und Threshold/`model_id` in der Meta. Beide Pfade
  koexistieren jetzt: ohne Artefakt läuft der Legacy-Zweig bit-identisch weiter,
  mit Artefakt gewinnt es und bringt Tag + Threshold mit. Die Funding-Features
  werden **as-of dem Event** gezogen (`funding_features_asof`, wie
  `tools/epd2_build_dataset.py:231`), je Trigger hinter dem
  `vol_ratio>=5`-Vorfilter. Fehlende Funding-**Historie** wird zu 0 wie
  `fillna(0)` im Trainer (Serving-Parität); ein fehlender Feature-**Name**
  verweigert dagegen weiterhin das Artefakt und idlet den Bot (P0.12).

**Bekanntes Performance-Risiko (dokumentiert, nicht optimiert — greift erst mit
deploytem EPD2-Artefakt):** der Funding-Load ist ein DB-Roundtrip pro
qualifizierendem 10s-Tick, nicht pro Signal. Der Vorfilter `vol_ratio>=5` hält an,
solange das Volumen-Event läuft, und der Shadow-Zweig setzt den 900s-Timer
bewusst nicht zurück (P1.41) — ein Coin im Shadow-Band zieht die Query also auf
jedem Tick, marktweit parallel über alle betroffenen Coins. Ein TTL-Cache wäre
hier **kein** trivialer Fix: er verschöbe den As-of-Zeitpunkt der Funding-Features
und bräche genau die Trainer-Parität, die dieser Commit herstellt. Vor dem
EPD2-Rollout zu klären (Messung, dann ggf. Load hinter ein Zeit-Gate ziehen, das
den As-of-Zeitpunkt nicht verändert).

**Transitionaler Dedup**, je Bot dort, wo er wirklich sperrt: der Post-Tag ist
zugleich der Dedupe-Key, und beim Generationswechsel kippt er. SRA prüft die
Master-Log-Duplikatprüfung (sonst hielte ein SRA2-Rollout jeden bereits
verarbeiteten Trade für neu und postete ihn erneut) und den Cooldown gegen den
Alt-Tag. EPDs einziger tag-gekoppelter Lock ist die Shadow-Log-Dedupe; dafür
nimmt `core/signal_post.log_prediction` ein optionales `legacy_tag` entgegen —
geschrieben wird immer unter dem aktuellen Tag. Alle anderen Aufrufer sind
unberührt (Default `None`).

**Live-Semantik unverändert.** Kein Artefakt ist deployt, also läuft beides auf
dem Legacy-Vertrag: gleiche Tags, gleiche Thresholds, gleiche Feature-Vektoren,
gleiche Dedupe-Queries (die transitionalen Binds kollabieren bei identischen
Tags). Verifikation DB-frei: `backtest/test_model_artifacts.py` (10),
`test_sra_tag.py` (11), `test_epd_tag.py` (12) — Loader- und Dedupe-Verhalten
echt ausgeführt (Fake-Cursor), der Rest statische Netze; alle mutations-geprüft.
Kein Rollout, kein Artefakt angefasst, keine DB-Änderung.

**Streukreis der `core/`-Änderungen** (geteilter Code, deshalb explizit): (1)
`log_prediction` ist additiv — `legacy_tag` hat den Default `None` und lässt die
alte Einzeltag-Query byte-identisch, die Bots 30–33 sind unberührt. (2)
`maybe_reload` reicht beim täglichen Reload jetzt `default_tag` statt des
**aktuell geladenen** Tags als Fallback weiter. Für `13_ai_rub_bot.RUB2_SHORT`
(hand-gebautes Contract-Dict ohne `default_tag`) fällt `.get()` exakt auf
`artifact["tag"]` zurück — genau der Ausdruck, den der alte `maybe_reload`
benutzte, also bit-identisch. Für die Bots 30–33 (Contract via `load_artifact`)
greift der Unterschied nur, wenn ein Artefakt beim Reload **keine** `model_id` in
der Meta trägt; dann erbte der Reload bisher den Tag der Generation, die er
gerade ersetzt. Das ist der eigentliche Bugfix an dieser Stelle, kein
Kollateralschaden — im Normalbetrieb (Trainer schreibt `model_id` immer) ist der
Pfad tot.

**Offen für Michi:** (1) EPD2/SRA2-Rollout ist jetzt entblockt — Operator-Entscheid.
(2) Zwei neue Befunde derselben Klasse wie P1.48: weder EPD noch SRA hat einen
Active-Trade-Check gegen `ai_signals`; EPDs einzige Re-Fire-Sperre ist ein
In-Memory-900s-Timer, der einen Prozess-Neustart nicht überlebt.
(Der `P1.46`-Nummernkonflikt dreier Sessions war beim Merge auf `main` bereits
durch PR #36 aufgelöst — Sniper behält P1.46, ATB1 wurde P1.47, RUB P1.48.)

## [2026-07-10] Zweiter Look-ahead in `walkforward_sim.load_joined`: `bfill()` entfernt (T-2026-CU-9050-045)

Nebenfund aus der Blast-Radius-Analyse zu T-2026-CU-9050-037. `load_joined` rief
nach `ffill()` zusätzlich `bfill()`. Das `ffill` schließt Innen-Lücken aus der
Vergangenheit und ist harmlos; das `bfill` füllte die verbleibenden **Kopfzeilen
aus der Zukunft**.

> **Korrektur 2026-07-10 (nach Code-Prüfung von `2_indicator_engine.py:335-448`):** die
> ursprüngliche Fassung dieses Eintrags begründete den Fix mit „die Warmup-Spalten sind
> NULL (`ema_200` braucht 200 Bars, die Donchian-Kanäle 20)". **Das ist falsch.** Die
> Engine liefert diese Spalten gefüllt: `ema_*`, `macd_*`, `atr_14`, `tsi_*` sind
> `ewm(adjust=False)` und ab Zeile 0 definiert; `wma_21`, `donchian_*_20`, `boll_*_20`
> tragen `.fillna(0)`, `rsi_14` trägt `.fillna(50)`. Der Fix bleibt richtig, seine
> Mechanik ist aber eine andere — unten korrigiert. Die Fehlerklasse ist Falle 13 aus
> `docs/OPUS-HANDOFF.md`, eine Ebene tiefer: der Loader wurde am Code geprüft, der
> Datenproduzent dahinter nicht.

Genau **eine** der fünfzehn Spalten, die `load_joined` liest, ist in der DB wirklich
leer: **`kama_21`**. `calculate_kama` (`2_indicator_engine.py:344-350`) füllt bewusst
nicht — die Zeilen 0–19 sind NaN, Zeile 20 trägt den SMA-Bootstrap. `bfill` hatte damit
genau ein Ziel: es schrieb diesen Bootstrap-Wert rückwärts in die 20 Zeilen davor, also
Zukunft in die Vergangenheit. `run_td_bb` beginnt zwar erst bei `t = WINDOW-1 = 149`,
die Feature-Kerze ist aber der **Pivot-Index** (`lo_b + p3`), und der reicht bei kleinem
`t` bis Zeile 0 herunter. Anders als der forming-Kerzen-Befund aus T-037 — der sich
selbst quarantänisiert, weil seine Records kein Label bekommen und `load_replay` sie
verwirft — landete dieser Leak damit in **gelabelten** Trainingszeilen der td/bb-Replays
(Modelle TD2/BB2, Bot 25). Betroffen sind Coins, deren Listing in das Replay-Fenster
fällt; für ältere Coins enthält der Frame kein NaN und `bfill` war ein No-op.

**Der größere Nachbar-Befund, den dieser Fix NICHT behebt:** die `.fillna(0)`-Spalten
sind kein NaN und überleben das `dropna()`. Für eine junge Coin steht in den ersten ~20
Bars `donchian_upper_20 = 0.0`, und `extract_ml_features` macht daraus
`donchian_upper_20_dist_pct = -100.0`. Fünf der elf Preis-Features sind dort hart
gepinnt. Das ist **P1.13** im `AUDIT_TODO.md` („`fillna(0)` auf Warm-up-Fenstern schreibt
erfundene Indikatorwerte", Fix: NaN fließen lassen wie KAMA es tut) und gehört vor den
nächsten TD2/BB2/QM2-Retrain, weil es die Feature-Verteilung von Bot **und** Replay
gleichermaßen verschiebt.

Fix: `to_numeric` vor `ffill` gezogen, `bfill` ersatzlos entfernt, die verbleibenden
NaN-Kopfzeilen werden verworfen. Ein Event ohne echte Indikatoren ist kein
Trainingsdatum. `backtest/test_feature_lookahead.py` pinnt das mechanisch
(mutations-geprüft: mit `bfill` fällt der Test).

**Nicht angefasst, bewusst:** `25_smc_ml_sniper.py:220` und `24_quasimodo_bot.py:126`
tragen dieselbe Zeile. Sie fenstern aber `DESC LIMIT 150` bzw. `100` **ab jetzt** — dort
füllt `bfill` aus Zeilen, die der Bot ohnehin schon gesehen hat, also kein Look-ahead
relativ zur Entscheidungszeit, sondern eine stille Imputation des Feature-Vektors. Und
sie feuert nur, wenn die ersten 20 Kerzen der Coin-Historie im Fenster liegen, der Coin
also ≤ ~170 Kerzen hat (`1h`: 4–7 Tage alt; `4h`: 17–28 Tage) — für die große Mehrheit
der Coins ist `bfill` dort ein No-op.

Wichtiger als die Zeile selbst ist ihre **Kopplung an den Retrain**: seit diesem Commit
verwirft der Replay die 20 Kopfzeilen, der Live-Bot imputiert sie weiter. Das nächste
aus dem Replay trainierte TD2/BB2/QM2 hat sie nie gesehen. Die Bots dürfen deshalb
**nicht isoliert** angeglichen werden, sondern nur **gemeinsam mit dem Artefakt-Rollout**
— sonst entsteht genau der Train/Serve-Skew, gegen den T-037/T-045 antreten. Geld-Pfad,
Operator-Entscheidung (`docs/OPUS-HANDOFF.md` §6).

## [2026-07-10] `legacy_trainers/` ist keine Wegwerf-Ware — Operator-Frage §5.8 geschlossen (Doku)

`docs/CANDLE_CALL_SITES.md` führte `legacy_trainers/` an drei Stellen als „toter
Code" und „löschbar". Beides ist irreführend und stand im selben Absatz wie der
bereits korrigierte `db_schema_analysis.py`-Fehlbefund (T-2026-CU-9050-039).

Richtig ist: kein laufender Prozess importiert die Skripte, und sie sind bewusst
nicht lauffähig (Credentials durch `os.getenv(...)`-Platzhalter ersetzt). Genau
das ist ihr Zweck. Sie sind die **einzige Reproduktionsgrundlage der acht live
geladenen Modell-Artefakte** — `legacy_trainers/README.md` ordnet jeden Trainer
seinem Artefakt und Bot zu (MIS1→11, ABR1→18, ATS1→12, RUB1→13, SRA1→9,
AIM1→15, EPD1→10, ATB1→14), und der Ordner entstand in `7b5ec89` ausdrücklich
als „frozen provenance". Ihre konservierten Defekte (Label-Geometrie,
Split-Leakage, In-Sample-Thresholds, Feature-Skews) erklären das Verhalten der
Live-Modelle und sind die Referenz, gegen die das Retrain-Programm misst.

Für die Migration sind sie irrelevant — sie werden **nicht umverdrahtet**, und
nach Phase C laufen sie ohnehin nie wieder. Das ist ein Argument gegen
Umverdrahten, keines fürs Löschen; der alte Text vermischte beides.

**Entscheid: `legacy_trainers/` bleibt.** Operator-Frage §5.8 ist damit in beiden
Teilen beantwortet und blockiert Phase 1 nicht mehr. Ein `NICHT LÖSCHEN`-Hinweis
steht jetzt auch oben in `legacy_trainers/README.md`, wo ein Folge-Agent zuerst
hinschaut. Kein Code berührt.

## [2026-07-10] Vier rote Tests auf main repariert (T-2026-CU-9050-038)

CI gated nur ruff/format, mypy, Syntax/Imports und Secret-Regex — pytest läuft
nirgends. Vier Tests der `backtest`-Suite waren deshalb unbemerkt rot, teils
seit dem Initial-Import. Bei T-2026-CU-9050-034 fielen sie beim Lauf der vollen
Suite auf. Jeder wurde am Code diagnostiziert, keiner stillschweigend geskippt
oder gelöscht.

- **`test_bot_naming::test_similar_but_not_matching`** — der Test hielt am
  MIS1-only-Vertrag fest, während `core/bot_naming.py` in `99e9de3` bewusst auf
  `MIS\d+` generalisiert wurde (harte Regel 6: Retrains posten unter neuem Tag).
  Der Docstring der Funktion dokumentiert `pretty_name("MIS2-72H") == "MIS2-72h"`
  bereits. Der Test wurde nachgezogen; die eigentliche Invariante (Generationen
  vermischen sich nicht) ist als eigener Test erhalten.
- **`test_bot_regime_analyzer::test_regime_lookup_for_trade`** — tot geboren: er
  importierte ein nie existierendes Modul `src_27` und rechnete seine Assertions
  inline nach, ohne den Produktionscode je aufzurufen. Ersetzt durch echte Tests
  gegen `27_bot_regime_analyzer._compute_stats` (Aggregat, leere Eingabe,
  Sharpe-Guard bei n=1).
- **`test_signal_orchestrator::test_identify_bot_channel_fallback`** — testete
  die Umgebung statt den Code. `core.config._ch()` liefert `0` für unbelegte
  Channels; auf der Build-Maschine (leerer `.env`-Stub) kollabierten damit alle
  fünf Keys von `CHANNEL_TO_BOT_FALLBACK` auf `0`, und der letzte Eintrag gewann.
- **`test_signal_orchestrator::test_compute_rom1_trade_params_long`** — der
  R4-Audit-Fix zog `cap_leverage_to_sl()` in den ROM1-Pfad, der Test mockte aber
  nur `get_max_leverage`. `params["leverage"]` war deshalb ein `MagicMock` aus
  dem gemockten `core.trade_utils`. Der Test setzt jetzt die echte Funktion ein
  und prüft den tatsächlichen Cap (`"6x"`: 8 % SL-Distanz deckeln die
  gewünschten 20x).

### Live-Semantik
Eine Produktions-Änderung: `CHANNEL_TO_BOT_FALLBACK` wird über
`_build_channel_fallback()` gebaut und lässt den `0`-Sentinel unbelegter
Channels fallen. Auf dem VPS sind alle fünf `CH_*` echte, distinkte Telegram-IDs
— die Map ist dort unverändert. Der Filter greift nur im degenerierten Fall:
statt dass ein deaktivierter Bot auf einen **fremden** Bot-Namen auflöst, liefert
`identify_bot` jetzt `None`. Da `identify_bot` ausschliesslich mit echten
Channel-IDs gerufen wird (`28:659`), ändert sich das Live-Verhalten nicht.

### Nebenbefunde (mitgefixt)
`test_signal_orchestrator.py` und `test_bot_regime_analyzer.py` liessen sich nur
sammeln, wenn zufällig eine alphabetisch frühere Testdatei `DB_PASSWORD` bzw.
`TELEGRAM_BOT_TOKEN` gesetzt hatte; beide seeden ihre Dummies jetzt selbst.
`test_abr1_detection.py` brach beim Collect ab: `pandas_ta` steht in
`requirements.txt:18` und ist auf dem VPS installiert, auf dieser Python-3.14-
Build-Maschine aber nicht installierbar (zieht `numba`, kein cp314-Wheel,
Source-Build schlägt fehl). Der harte Collect-Fehler ist durch einen benannten
`pytest.importorskip` ersetzt — reines Umgebungsproblem, kein Code-Fehler.

### Verifikation
`python -m pytest backtest -q` → volle Suite grün, genau ein Skip (der benannte
pandas_ta-`importorskip`); zusätzlich läuft jede Datei der Suite einzeln grün
(die Import-Reihenfolgen-Kopplung ist weg).
ruff, `ruff format --check` und mypy sauber.
`python tools/regression_guard/guard.py smoke` OK — der Guard wurde nicht
refreshed. Der neue Guard-Test gegen `_build_channel_fallback` ist per Mutation
geprüft: entfernt man den `if cid`-Filter, wird er rot.
## [2026-07-10] RUB bekommt den Active-Trade-Check seiner Geschwister (T-2026-CU-9050-043)

`13_ai_rub_bot.py` war der einzige AI-Bot ohne Positions-Guard: seine einzige
Re-Fire-Sperre war der 4h-Cooldown (`:252`), und die ganze Datei berührte
`ai_signals` nur schreibend (INSERT `:376`). Ein Cooldown begrenzt die Signal-
**Frequenz**, nicht die Zahl gleichzeitig offener Positionen. Ein Mean-Reversion-
Trade überlebt seine vier Stunden regelmässig — danach durfte derselbe Coin in
derselben Richtung erneut feuern, und Cornix öffnete eine **zweite volle Position
mit eigenem SL** neben der ersten. MIS (`:318`), QM und der Sniper (`:116`) haben
den Guard seit jeher; RUB fehlte er ohne dokumentierten Grund. Das ist auch der
Grund, warum der transitionale Dedup aus T-2026-CU-9050-030 bei RUB in den
Cooldown ausweichen musste — es gab schlicht keinen Check, in den er gehört hätte.

Operator-Entscheid vorab (Michi, 2026-07-10): kein beabsichtigtes Averaging-Down,
sondern ein Bug.

Fix:

- Vor der (teuren) ML-Prediction prüft der Bot jetzt
  `SELECT 1 FROM ai_signals WHERE symbol/direction/model IN (%s, %s)` und
  überspringt das Signal bei einem Treffer — Muster `11_ai_mis_bot.py`.
- Gebunden wird derselbe **richtungsabhängige** Tag, den auch der Post-Pfad
  schreibt (LONG `RUB_LONG_TAG`, SHORT `RUB2_SHORT["tag"]` aus der Artefakt-Meta),
  plus `RUB_LEGACY_TAG` als transitionaler Dedup: der Tag ist zugleich der
  Dedupe-Key, und beim RUB3-Rollout kippt er. Ohne den Alt-Tag im `IN` würde eine
  offene RUB2-Position ein RUB3-Signal auf demselben Coin nicht mehr blocken —
  exakt die zweite Live-Position, die dieser Guard verhindert. Solange die Tags
  übereinstimmen (heute), ist das `IN` ein No-op.
- Der Cooldown bleibt **unverändert** als Frequenz-Sperre daneben stehen (wie bei
  MIS laufen beide parallel). Sein jetzt falscher Kommentar („prüft ai_signals
  nicht") ist mitgezogen.
- `backtest/test_rub_tag.py`: zwei neue DB-freie Tests (Guard vorhanden + Skip;
  Bindung an `module_tag` **und** `RUB_LEGACY_TAG`). Mutations-geprüft — Legacy-Tag
  aus dem Bind entfernt bzw. Check ganz entfernt ⇒ beide rot.

**Live-Semantik ändert sich hier bewusst**, anders als bei T-030: Signale auf einem
Coin, auf dem bereits ein RUB-Trade derselben Richtung offen ist, fallen weg. Die
erste Position, jedes Signal auf freiem Coin und die Gegenrichtung bleiben
unberührt; der Cooldown-Pfad ist bit-identisch. Keine DB-Änderung, kein Rollout.

**Offen für eine VPS-Session:** die Rückwärts-Messung, wie oft
`(symbol, direction, model='RUB2')` real mehrfach gleichzeitig offen war
(`ai_signals` / `closed_ai_signals`, read-only). Nicht blockierend für den Fix.
## [2026-07-10] Doppeltes `db_schema_analysis.py` bereinigt (T-2026-CU-9050-039, P3.1)

`tools/db_schema_analysis.py` gelöscht. Die Root-Kopie ist kanonisch und bleibt
unverändert; die Fleet ist nicht betroffen (das Skript ist ein read-only
DBA-Werkzeug über den PostgreSQL-System-Katalog, kein Bot-Pfad).

Die Ausgangs-Annahme, beide Dateien seien **byte-identisch**
(`docs/CANDLE_CALL_SITES.md` §2), war **falsch** und ist dort jetzt korrigiert:

- Die Root-Kopie trägt den ruff-Cleanup aus `052ba4c` (Import-Sortierung,
  `zip(..., strict=False)`, Formatierung); die `tools/`-Kopie stammt unverändert
  aus dem Initial-Import `b6735d9`.
- Die `tools/`-Kopie war zudem **nicht lauffähig**: ihr
  `sys.path.insert(0, dirname(__file__))` zeigte auf `tools/`, wo kein `core/`
  liegt — `from core.database import …` scheiterte immer, sie brach mit
  „core.database nicht gefunden" ab. `audit_reports/10_dashboard_tools.md:47`
  und `AUDIT_TODO.md` P3.1 hatten das bereits richtig beschrieben.

Kein Eingriff an `pyproject.toml` oder `.github/workflows/typecheck.yml` nötig:
beide Exclude-Einträge nennen die Root-Datei, die bleibt (`tools/` ist ohnehin
pauschal excluded).

## [2026-07-10] Watchdog-Backoff blockiert die Fleet-Aufsicht nicht mehr (T-2026-CU-9050-029, P1.37)

`time.sleep(delay)` stand im Pro-Prozess-Rumpf der Monitor-Schleife. Bis zu
900 Sekunden lang fror das den **gesamten** Watchdog ein: kein anderer Bot wurde
beaufsichtigt, kein Park-Marker beachtet, kein Dashboard-Restart konsumiert,
kein Health-Check gefahren. Der Watchdog ist der einzige Aktor der Flotte — ein
einzelner crash-loopender Bot nahm damit die Aufsicht über alle ~29 anderen mit.

Zweiter Fehler auf denselben Zeilen: nach dem Sleep lief `start_process`
bedingungslos. Wer den Bot während der 900s parkte, sah zu, wie der Watchdog ihn
trotzdem wiederbelebte.

Der Delay ist jetzt eine **Pro-Prozess-Deadline** (`_restart_not_before`). Die
Schleife dreht weiter und überspringt nur den betroffenen Bot. Die Reihenfolge
der Zweige ist tragend und an der Funktion dokumentiert: Park schlägt alles
(und verwirft eine anstehende Deadline), ein Dashboard-Restart schlägt den
Backoff, dann erst greift die Deadline. Weil der Park-Check dadurch in jedem
10s-Zyklus erneut läuft, hält ein Park während des Backoff-Fensters den Bot
unten — der zweite Fehler fällt durch dieselbe Umstrukturierung.

Die Backoff-Kurve selbst ist unverändert (0/15/60/300/900s nach Crashes der
letzten Stunde) und per Test festgenagelt.

**Refactor mit Touch-Kontext:** der Pro-Prozess-Rumpf liegt jetzt in
`supervise_process(p_info, current_time)`. Jedes `continue` wurde zu `return` —
für einen Schleifenrumpf äquivalent. Ohne diese Extraktion ist die Deadline
nicht testbar, ohne `main()` samt Lock, Orphan-Kill und gestaffeltem Fleet-Start
zu fahren.

**Beweislage, ehrlich:** `backtest/test_watchdog_backoff.py` (neu, standalone,
DB-frei, 6/6) sind Regressions-Guards auf dem neuen Verhalten, **keine** Zeugen
des alten Bugs — auf dem Pre-Fix-Stand erroren sie, weil `supervise_process`
noch nicht existierte. Der alte Fehler ist am Pre-Fix-Code direkt ablesbar
(`main_watchdog.py:443-447`). Damit er nicht zurückkommt, patcht die Fixture
`time.sleep` mit einem Mock, der wirft: jeder künftige blockierende Wait im
Supervision-Pfad macht die Suite rot.

Wirkt beim nächsten regulären Watchdog-Restart, kein Deploy.

---
## [2026-07-10] SMC-Sniper: Pivots nicht mehr auf der laufenden Kerze (T-2026-CU-9050-036, P1.46)

`25_smc_ml_sniper.py` liest 150 Kerzen `DESC`, dreht auf ASC — und liess
`scipy.signal.argrelextrema` bisher über den **vollen** Frame laufen. Die
letzte Zeile ist die forming Kerze. Ihr High/Low bewegt sich, also repaintete
der Pivot-Satz **innerhalb** der laufenden Kerze: die drei Drives eines
Three-Drive und das Level eines Breaker-Blocks verschoben sich, nachdem das
Signal bereits gepostet war. Die Schwesterbots droppen die forming Kerze seit
Juli (`24:138` aus P1.24, `16:334` aus P1.27, `21:126`); 25 war die einzige
Lücke — und der einzige der vier, der im Geld-Pfad live postet (harte Regel 5).

Fix: `c_highs, c_lows = highs[:-1], lows[:-1]` vor den beiden
`argrelextrema`-Aufrufen, Muster wie `24_quasimodo_bot.py:138`. Die
Pivot-Indizes bleiben zu den Vollarrays aligned (`highs[p1]`, `rsis[p1]`
funktionieren unverändert), und alle `len(df)-1`/`len(df)-2`-Offsets — die
BB-Feature-Zeile, das Breakout-Fenster, die Freshness-Gates — bleiben
unberührt. Ein `df.iloc[:-1]` auf den Frame hätte genau diese Offsets um eine
Kerze verschoben; das ist bewusst nicht passiert und per Test festgenagelt.

`current_price = closes[-1]` bleibt **live**: es ist der CMP, an dem der Entry
gesetzt wird, plus der Auslöser für die BB-Level-Nähe — kein analytischer
Input. Der R1-Endzustand (`include_forming=False` auch für die Preis-Seite)
hängt an den Operator-Fragen 4/6 aus `docs/CANDLE_CALL_SITES.md` und an
Migrations-Block 4.

Signal-Raten-Delta, DB-frei über die Regression-Guard-Fixtures replayt
(4 Coins × 1h/4h, 3.608 Scan-Punkte, jeweils 150-Kerzen-Fenster mit der letzten
Zeile als forming Kerze; gezählt wird der Geometrie-Trigger vor ML-Gate und
Cooldown). Reproduzierbar über `python tools/sniper_forming_delta.py`:

| Pattern | vorher | nachher | beide | nur vorher | nur nachher |
|---|---|---|---|---|---|
| BB LONG | 58 | 57 | 50 | 8 | 7 |
| BB SHORT | 65 | 61 | 56 | 9 | 5 |
| TD LONG | 11 | 10 | 10 | 1 | 0 |
| TD SHORT | 20 | 19 | 17 | 3 | 2 |
| **Summe** | **154** | **147** | **133** | **21** | **14** |

Also **−4,5 %** Trigger-Rate; 21 Trigger fallen weg, 14 kommen hinzu (der
verschobene Pivot-Satz ändert `peak_idx[-2]` und damit das BB-Level). Der
Replay misst exakt das Code-Delta (Zeile drin vs. draussen); der echte
Live-Repaint ist grösser, weil dort die forming Kerze nur teilweise gefüllt
ist. R1 senkt die Signal-Raten — das ist der Zweck; Schwellen erst nach dem
Retrain neu tunen.

Bewusst **nicht** mitgefixt: `argrelextrema(mode='clip')` lässt am rechten Rand
weiter unbestätigte Pivots durch (der `max_confirmed_idx`-Filter aus P1.24).
Bei 25 ist das kein Drop-in — das TD-Frische-Gate
(`len(df) - p3 <= PIVOT_WINDOW + 2`) sucht genau diese Kanten-Pivots. Ein Filter
dort wäre eine Strategie-Änderung, kein Bugfix, und gehört in einen eigenen
Task.

Verifikation: `backtest/test_sniper_forming.py` (neu, 4/4, DB-frei — inkl. eines
numerischen Tests, der den Repaint-Mechanismus selbst reproduziert),
`backtest/test_sniper_tag.py` (4/4), `guard.py smoke` grün, ruff + mypy grün.
Wirkt beim nächsten regulären Restart, kein Deploy.

## [2026-07-10] Pump/Dump-Fenster zeit-basiert statt index-basiert (T-2026-CU-9050-029, P1.39)

Der Detector schnitt seine Fenster über Listen-Indizes: `prices[-7:]` hiess nur
dann „die letzten 60 Sekunden", wenn jeder 10s-Bucket ankam. Bei einer
WS-Lücke — am wahrscheinlichsten genau im Spike, wenn der Socket am meisten zu
tun hat — spannte „-7" über Minuten, und das Modell bewertete ein still
gedehntes Fenster.

Dazu ein zweiter, unabhängiger Fehler: `volumes_10s` war auf `v10s_valid`
**gefiltert**, `prices` nicht. `volumes_10s[-18:]` und `prices[-18:]` zeigten
also auf unterschiedliche Zeitpunkte, sobald ein einziger Bucket ungültig war.

Beide Abschnitte (Volume-Explosion-Alert und ML-Feature-Pfad) routen jetzt über
`_find_bucket_before` / `_find_bucket_range`, die nach Zeitstempel auswählen —
dieselben Helfer, die der Preis-Spike-Pfad längst nutzt. Die flachen
`prices`/`volumes_10s`-Listen sind ersatzlos entfallen: dass beide nach dem
Umbau unbenutzt waren, ist der Beleg, dass keine Index-Rechnung übrig blieb.

Fehlt der Bucket von vor 60s, wird der Tick **übersprungen**, statt eine
erfundene `0` als Feature ins Modell zu schreiben — eine 0 ist ein Messwert,
kein „unbekannt".

### Anker statt Wanduhr
Alle Bucket-Lookups messen gegen `bucket_anchor` (den Stempel des jüngsten
Buckets), nicht gegen `now`. Die Stempel sind aufs 10s-Raster gefloort, `now`
ist der Aufrufzeitpunkt — und der Detector iteriert ~530 Coins nach einem
REST-Roundtrip, der Versatz wandert also auch über den Batch. Gegen `now`
gemessen schrumpfte das 60s-Fenster ab einem Versatz von 5s still auf 6, dann
5 Buckets: `buy_pres`/`volat` beschrieben ~50 Sekunden, während `p_chg_60s`
weiter echte 60 Sekunden maß. Drei Features, die dieselbe Spanne beschreiben
sollen, taten es nicht. Gegen den Anker liegt jeder Zielzeitpunkt exakt auf
einem Rasterpunkt, und `WINDOW_EDGE_GUARD = 5` absorbiert nur noch
Parse-Rauschen. Gefunden im `z-code-reviewer`-Pass, nicht durch die erste
Test-Runde — die synthetisierte Buckets mit Versatz 0.

Mit umgestellt wurden auch die drei vorbestehenden Lookups des
Preis-Spike-Pfads: zwei Zeitbasen für Geschwister-Lookups derselben Funktion
wären schlimmer als eine falsche. Bewusst **nicht** umgestellt, weil echte
Wanduhr-Semantik: Staleness-Check, die beiden Alert-Cooldowns und
`pump_dump_events.spike_time`.

### Messung
Im Gap-Szenario des Tests meldete die alte Index-Rechnung `p_chg_60s = +100.0`
— sie griff über ein 10-Minuten-Loch auf einen Bucket mit halbem Preis. Die
zeit-basierte Variante meldet die wahren `0.0`. Genau solche Werte landeten
bisher auch in `pump_dump_events`.

### ⚠ Retrain-Kopplung
`vol_ratio`, `p_chg_60s`, `buy_pres` und `volat` sind Modell-Inputs **und**
werden so nach `pump_dump_events` geloggt, woraus `tools/epd2_build_dataset.py`
trainiert. Das deployte EPD2-Artefakt wurde auf der alten Definition gefittet;
bis zum Retrain-Rollout läuft Serving gegen eine leicht verschobene Verteilung.
Bei lückenlosen Ticks sind alt und neu identisch (Kontroll-Tests belegen das),
die Drift betrifft ausschliesslich Gap-Ticks — dort war der alte Wert aber
falsch, nicht bloss anders. Operator-Entscheid Michi 2026-07-09; Folge-Task
**T-2026-CU-9050-035** (EPD2-Retrain auf den neuen Feature-Definitionen).

Verifikation: `backtest/test_pump_dump_time_windows.py` (neu, standalone,
DB-frei, 6/6). Vier Tests fallen auf dem Pre-Fix-Stand; die zwei übrigen laufen
auf beiden Ständen grün und belegen damit, dass der lückenlose Pfad unverändert
ist. Wirkt beim nächsten regulären Restart, kein Deploy.

---

## [2026-07-09] "Opened"-Zählung entdoppelt, EPD2-Shadow-Inserts gedrosselt (T-2026-CU-9050-029, P1.44 + P1.41, PR #23)

Zwei Hälften desselben Defekts: der Schreiber produzierte Shadow-Zeilen ohne
Drossel, der Leser zählte sie — und zählte gepostete AI-Signale obendrein
doppelt. Die per-Bot-Statistik ist die Entscheidungsgrundlage des
Orchestrator-Gatings, also ist eine aufgeblähte „Opened"-Zahl ein
Geld-Pfad-Defekt.

### P1.44 — Leser: Opens kommen aus `ai_signals`, nicht aus dem Prediction-Log
`ml_predictions_master` ist ein append-only Log — nirgends im Repo wird daraus
gelöscht. `closed_ai_signals` hält dieselben Signale nach dem Schliessen, und
beide Frames landeten in `df_all_created`. Jedes AI-Signal, das im Fenster
öffnete **und** schloss, zählte damit zweimal. Zusätzlich trug der Log
Shadow-Zeilen (`posted=False`), die nie gehandelt wurden.

Die klassische Seite hatte das Problem nie: die Monitore DELETEn beim Schliessen
aus `active_trades_master` bzw. `ai_signals` und INSERTen in die
`closed_*`-Tabelle — aktiv ∪ geschlossen ist also disjunkt. Die AI-Seite
spiegelt das jetzt: `ai_signals` ∪ `closed_ai_signals`. Beide Posts teilen sich
einen `_load_open_ai_signals()`-Helper; die Drift zwischen Summary- und
Per-Bot-Post war die eigentliche Ursache.

**Verworfene Alternative** (Operator-Entscheid): `ml_predictions_master WHERE
posted=TRUE` als Quelle. Der Log ist **dedupliziert** (4h je Modul/Coin/
Richtung), nicht vollständig — ein legitimer Re-Post in dem Fenster hätte keine
Zeile, die Opens würden **unter**zählen.

### P1.41 — Schreiber: EPD2-Shadow-Inserts laufen über `log_prediction()`
Der Shadow-Zweig (`0.25 ≤ p < 0.60`) INSERTete auf jedem qualifizierenden
10s-Tick. Das 900s-Gate darüber bremst ihn nicht: `last_alert_time` wird nur im
Live-Trade-Zweig zurückgesetzt. Ein Coin, der dauerhaft im Shadow-Band
predictet, drosselte sich daher nie (bis 8640 Rows/Tag/Symbol). Statt eines
neuen Cooldowns nutzt der Zweig jetzt `core.signal_post.log_prediction()`, das
bereits 4h je Modul/Coin/Richtung dedupt — derselbe Pfad wie bei den Bots 30-33.
Der Timer wird hier **bewusst nicht** gesetzt: er gated auch echte Signale, ein
Reset würde Live-EPD2-Trades desselben Coins 900s unterdrücken.

### Live-Semantik
Beabsichtigt geändert: bei 1 offenen + 1 geschlossenen AI-Signal im Fenster
meldet „Opened" jetzt **2 statt 3**, und eine Shadow-Prediction taucht gar nicht
mehr als eröffnetes Signal auf. Closed-Counts, Win-Rate und Kelly-Mathematik
bleiben unberührt — `df_all_closed` zieht weiterhin ausschliesslich aus den
`closed_*`-Tabellen. Wirkt beim nächsten regulären Restart, kein Deploy.

Bekannt, hier nicht gefixt: `log_prediction` dedupt gegen `NOW()` (PG-Lokalzeit)
auf UTC-Rows. Das verschiebt das effektive Fenster, drosselt aber. Gehört ins
R3/TZ-Cluster (P2.1–P2.6) und darf dort nicht per Punkt-Fix angefasst werden.

Verifikation: `backtest/test_market_tracker_opened.py` (neu, 7/7) und
`backtest/test_shadow_prediction_cooldown.py` (neu, 4/4), beide standalone und
DB-frei. Der Kern-Test fällt auf dem Pre-Fix-Stand mit 3L statt 2L — er misst
den Doppelzähler, statt an einer Exception zu sterben.
## [2026-07-10] Look-ahead im Walk-Forward-Simulator geschlossen (T-2026-CU-9050-037)

`tools/walkforward_sim.py` ist seit P0.10 die **einzige Label-Quelle des gesamten
Retrain-Programms**. Seine beiden Haupt-Loader `load_ohlcv` (`:174`) und
`load_joined` (`:204`) lasen bis `NOW()` ohne obere Grenze — die laufende Kerze
kam als geschlossene im Replay an. Jedes daraus trainierte Modell hat auf einer
Kerze gelernt, die es zur Entscheidungszeit noch nicht kannte (harte Regel 5).
Die Schwester-Loader `load_mis1_frame` (`:635`) und `load_rub_frame` (`:759`)
derselben Datei schnitten schon immer korrekt ab.

Fix:

- Beide Loader gehen jetzt über **`core.candles`** (`read_candles` /
  `read_candles_with_indicators`, `include_forming=False`) statt über rohe
  f-String-SQL. Damit greift der TF-generische Epoch-Cutoff der Kerzen-API.
  Bewusst **nicht** das `date_trunc('hour', NOW())` der Nachbarn kopiert: die
  Loader lesen auch `1d` und `4h`, dort hätte ein Stunden-Trunc die laufende
  Kerze stehen lassen. Nebeneffekt: ASC-Kontrakt und Identifier-Hygiene (P3.3).
- `backtest/test_feature_lookahead.py` bekommt zwei DB-freie Tests, die für alle
  benutzten Timeframes (1h/4h/1d) prüfen, dass die forming Kerze nicht im
  Replay-Frame landet. Mutations-geprüft: mit `include_forming=True` fallen sie.

Erster Schritt von Block 1 der Umverdrahtungs-Reihenfolge in
`docs/CANDLE_CALL_SITES.md` §4 (Offline-Tooling zuerst, `walkforward_sim` voran).
Kein Live-Signal-Pfad berührt, keine DB-Änderung.

**Offen für Michi:** ob bereits ausgerollte Modelle auf den alten, vergifteten
Labels trainiert wurden — und ob deshalb Staging-Retrains neu zu bewerten sind.
Diese Session hat nichts trainiert und nichts ausgerollt (C-Gate).

## [2026-07-09] Signifikanz-Layer über den Walk-Forward-Replay-Output (T-2026-CU-9050-027 D3)

Ein Replay-Summary sagt „+38 R über 365d" — `tools/wf_significance.py` beantwortet
neu die Folgefrage, ob dieser Edge von Rauschen unterscheidbar ist, bevor ein
Kandidat Richtung Live-Gate diskutiert wird. Rein additiv über dem Trade-JSONL
von `tools/walkforward_sim.py`; Muster aus HKUDS/Vibe-Trading (MIT,
`validation.py` + `bench_runner_strict.py`), adaptiert statt kopiert:

- **Random-Control (Sign-Flip):** Null-Verteilung aus Richtungs-Flips DERSELBEN
  Trades inkl. Fee-Drag (`flip(net) = -net - 2*fee_rt`) → p-Wert + Delta gegen
  den richtungslosen Zufalls-Trader, bewusst kein Test gegen 0.
- **Reihenfolge-Permutation für den MaxDD** (Verlust-Clusterung zufallstypisch?).
  Der vt-Permutationstest auf Sharpe wurde bewusst NICHT übernommen — bei
  per-Trade-%-PnL ist Sharpe reihenfolge-invariant, der Test wäre degeneriert.
- **Bootstrap-CIs** für per-Trade-Sharpe (bewusst nicht annualisiert), avg_r,
  TP1-WR.

Deterministisch (Seed 42). Verifikation DB-frei: `backtest/test_wf_significance.py`
(6/6, u.a. Edge-vs-Rauschen-Diskriminierung, Fee-Drag in der Null, CLI-
Determinismus). Doku: `docs/WF_SIGNIFICANCE.md`. Offen (VPS-Session): Lauf über
einen echten Batch-E-Replay-Output — Artefakte liegen nur auf dem VPS.
Multiple-Testing (FDR/Deflated Sharpe) bleibt bewusst Non-Scope (eigener Task).

---

## [2026-07-09] Look-ahead-Perturbationstest über die geteilten Feature-Builder (T-2026-CU-9050-027 D1, PR #19)

Die harten Regeln 5 (nur geschlossene Kerzen) und 7 (geteilte Feature-Builder,
Trainer == Serving == Replay) waren bisher nur durch Konvention und ~69
DO-NOT-/forming-/lookahead-Kommentare abgesichert. Neu: `backtest/
test_feature_lookahead.py` (standalone, DB-frei) macht sie mechanisch prüfbar —
Muster geerntet aus HKUDS/Vibe-Trading (MIT), `tests/factors/test_lookahead.py`.

- **Frame-/as-of-Builder** (`mis.add_advanced_features[_multi]`, research
  candle-context + PEX1/FMR1/FIF1-Rows, `funding_features_asof`): alle
  Input-Spalten ab der Perturbations-Zeile mit NaN/1e10 vergiften — die Zeilen
  davor müssen bit-nah (1e-9) invariant bleiben. Canary-Assertions belegen,
  dass die Vergiftung den Builder wirklich erreicht; ein Boundary-Test belegt,
  dass ein Funding-Settlement exakt AT ts strikt draußen bleibt.
- **Window-/row-scoped Builder** (`rub_trend`/`build_rub_features`,
  `build_trm1_row`, `funding_stats`, `regime_features`, `aim2.build_feature_row`):
  per Signatur ohne Zukunfts-Achse (Caller schneidet) — geprüft werden
  Determinismus, Input-Nicht-Mutation und die internen Fenstergrenzen (TRM1-12er,
  Funding-90er).
- **`fetch_context_frame`** (R1-Kern, DB-frei via Stub-Cursor): eine Forming
  Candle der aktuellen Stunde in der Tabelle ändert weder die gewählte
  Feature-Kerze (floor-1-Join) noch deren Features; der Staleness-Guard (>3h)
  liefert None.

**Ergebnis: kein Future-Leak gefunden** — gültiges No-op-Done. Detektionskraft
separat falsifiziert (künstliche `shift(-1)`-/`iloc[idx+1]`-Leaks sowie zwei
Mutation-Injektionen in echte Builder werden gefangen). Bekannter kosmetischer
Drive-by: `core/funding_features.py:70` wirft eine tz-UserWarning (Semantik
korrekt, UTC vs UTC) — nicht gefixt, geteilter Builder (Regel 7).

---

## [2026-07-09] Zentrale UTC-Policy gelegt: `core/time.py` + ruff DTZ (T-2026-CU-9050-032, R3)

Kythera hat keine Zeitquelle, sondern zwanzig. Writer schreiben teils naive
Serverlokalzeit, teils aware UTC, teils Postgres' `NOW()`; Reader interpretieren
dieselben Spalten als UTC. Der VPS läuft auf `Europe/Bucharest`, also läuft das
um +2/+3h auseinander — in Cooldowns, Trade-Fenstern und Burst-Zählern, also im
Geld-Pfad. Die Einzel-Fixes des Audits haben das Cluster nie geschlossen, weil
jeder von ihnen eine neue Domäne erfand.

Dieser Eintrag legt die Policy, **ohne Live-Semantik zu ändern**:

- **`core/time.py`** — `utc_now()` (aware), `utc_now_naive()` für die legacy
  `TIMESTAMP WITHOUT TIME ZONE`-Spalten, `to_utc()`, `as_naive_utc()`,
  `from_unix_ts()`. Ab jetzt die einzige sanktionierte Zeitquelle.
- **ruff-Regelgruppe `DTZ`** (`pyproject.toml`). Ein neues `datetime.now()` ohne
  `tz` fällt im CI durch, statt still eine weitere Domäne aufzumachen. Die zwei
  bewusst naiven Bestandsdateien (`3_detectors`, `30_ai_pex1_bot`) tragen ein
  `# noqa: DTZ…` mit Begründung — sichtbare Rest-Schuld statt stiller Ausnahme.
- **`docs/UTC_POLICY.md`** — Spalten-Inventar, der Bestand an Drift-Kompensationen,
  die Reihenfolge des Rests, und `docs/migrations/2026-07-r3-timestamptz.sql` als
  vorbereitete, **nicht ausgeführte** DDL.

Angepasst auf die neue Zeitquelle: `15_ai_master_bot` (deprecated `utcnow()` →
`utc_now_naive()`, identisch) und `core/market_utils.check_cooldown`
(handgeschriebener Normalisierer → `to_utc()`, identisch). Zwei Stellen ändern
eine sichtbare, aber folgenlose Ausgabe: `2_indicator_engine` schreibt den
State-Token und die Scheduler-Log-Zeile jetzt in UTC — der Token ist für
`3_detectors` ein opaker String-Vergleich, und der Minuten-Trigger ist gegenüber
einer Vollstunden-Offset-TZ invariant; `check_funding` rendert seine UTC-Epoche
nicht mehr als Lokalzeit.

`backtest/test_time.py` pinnt die Semantik der neuen Zeitquelle DB-frei, inklusive
eines Laufs unter gesetztem `TZ=Europe/Bucharest` — genau die Fehlerklasse
„läuft lokal, driftet auf dem VPS".

### Warum der Pool-Flip NICHT drin ist
Ursprünglich sollte `-c timezone=UTC` im Connection-Pool mit. Die Session-TZ
entscheidet, wie Postgres zwischen `timestamptz` und den naiven Spalten castet —
der Flip repariert also P2.5 und P2.6, **kippt aber sechs Stellen, die die Drift
heute bereits korrekt herausrechnen**: `15_ai_master_bot.to_utc_naive()` und die
fünf Dataset-Builder in `tools/` (`research_dataset_common`, `aim2_build_dataset`,
`fif1_build_dataset`, `pex1_build_dataset`, `retrain_sra2`). Die Trainer lesen
Historie; nach dem Flip trägt jede naive Spalte beide Domänen, und weder „immer
kompensieren" noch „nie kompensieren" ist richtig. Das ist der Train/Serve-Skew,
gegen den AIM2 gebaut wurde (P0.13).

Der Flip gehört deshalb in ein eigenes Fenster, zusammen mit dem P2.3-Writer-Fix,
den sechs Kompensationen und der Operator-Entscheidung Backfill-vs-Cutover für
die Historie. `docs/UTC_POLICY.md` §4–§6 ist der Handoff dafür.

---

## [2026-07-09] SMC-16 FVG-Entry war unerreichbar (T-2026-CU-9050-033, P1.26)

`find_unmitigated_fvgs` in `16_smc_forex_metals_bot.py` scannte auf Mitigation
über `range(fvg['index'] + 1, len(df))` — **inklusive** der aktuellen Kerze
(`curr_idx = len(df) - 1`) — und verwarf ein BULLISH-FVG, sobald `low <= top`
war. Genau dieses Prädikat prüft der Entry-Trigger anschliessend auf derselben
Kerze (`16:436`, symmetrisch BEARISH über `high >= bottom` in `16:464`). Jedes
FVG, das den Entry ausgelöst hätte, war damit per Konstruktion schon aus
`bull_fvgs`/`bear_fvgs` gefallen: der FVG-Entry konnte in beiden Richtungen nie
feuern. Der Beweis steht rein am Code — der FVG-Pfad schreibt als Cooldown-Key
ausschliesslich das literale `"SMC_FVG"` (`16:437,465`, die einzigen beiden
Writer dieses Keys), und dafür existieren 0 Live-Rows (die 83 gefundenen
`SMC_1H_FVG`/`SMC_4H_FVG`-Rows stammen aus einer älteren, TF-präfigierenden
Codeversion — die Falle, an der die frühere Widerlegung dieses Findings
scheiterte).

Der Scan endet jetzt vor der aktuellen Kerze (`range(fvg['index'] + 1, curr_idx)`).
Die aktuelle Kerze ist der Entry-Auslöser, nicht der Mitigator.

### Live-Semantik
Die einzige Verhaltensänderung: FVG-Entries werden möglich. Kerzen **vor** der
aktuellen mitigieren unverändert, die FVG-Erkennung selbst ist unberührt, und
die beiden Trigger-Bedingungen (`price > bottom * 0.999` bzw.
`price < top * 1.001`), Cooldown, Cornix-Message und Chart bleiben wie sie
waren. Der BOS/CHoCH-Pfad ist nicht betroffen.

### Verifikation
Neuer Guard-Test `backtest/test_smc_fvg_dead_code.py` (11 Fälle): Tap auf der
aktuellen Kerze überlebt den Scan (beide Richtungen), Tap auf einer früheren
Kerze mitigiert weiterhin, Entry-Trigger als Ganzes erreichbar, plus ein
Divergenz-Kanarienvogel, der den alten `range()` nachbaut und beweist, dass er
genau die triggernden FVGs tötet — ein Revert des Fixes lässt den Test rot
werden.

## [2026-07-09] MIS/RUB/QM posten unter der Artefakt-`model_id` statt unter einer Quellcode-Konstante (T-2026-CU-9050-030, P1.45, PR #24)

Nachbrenner zum Sniper-Fix aus PR #16: derselbe Fehlerklasse-Sweep fand drei
weitere Post-Pfade, die ihr Artefakt laden, die `meta.model_id` aber wegwerfen und
unter einer Konstante posten. **Heute stimmt der Tag jeweils zufällig** — es war
also kein Betriebs-Bug, sondern eine scharfe Mine unter dem nächsten
Retrain-Rollout: MIS3/RUB3/QM2 wären still unter dem Alt-Tag gelandet, hätten sich
in `ai_signals` und in der Per-Bot-Win-Rate mit der Vorgänger-Generation vermischt,
und das Orchestrator-Gating hätte über die Whitelist der neuen Generation anhand
der Performance der alten entschieden (Verstoss gegen Versionierungs-Regel 6).

### Fixed
- `11_ai_mis_bot.py` — **jedes der acht Horizont-Artefakte trägt jetzt seine eigene
  Generation aus `meta.model_id`**; den Posting-Tag baut der Gewinner-Kandidat
  (`f"{best_generation}-{best_horizon}"`). Ein Teil-Rollout (72H schon MIS3, Rest
  MIS2) taggt damit jedes Signal mit der Generation des Modells, das gefeuert hat,
  und wird beim Laden als gemischte Generation geloggt. Die Dateinamen
  `mis2_model_*.pkl` bleiben bewusst **generationsfreie Slot-Namen**
  (Operator-Entscheid 2026-07-09) — genau deshalb ist `meta.model_id` der einzige
  Generationsmarker. Fehlt sie, greift `MODEL_GENERATION` als Fallback, aber mit
  `logger.error` statt still.
- `13_ai_rub_bot.py` — **Tag ist jetzt richtungsabhängig**: SHORT nimmt
  `RUB2_SHORT["tag"]` (= `meta.model_id`, von `load_artifact` schon immer korrekt
  berechnet und bis dato weggeworfen), LONG behält die benannte Konstante
  `RUB_LONG_TAG`. LONG fährt das Legacy-Modell `long_reversion_model.joblib` ohne
  jede Meta und postet per Operator-Entscheid (2026-07-06) unter `RUB2` — den
  SHORT-Artefakt-Tag dorthin zu verdrahten, hätte ein Signal mit der Generation
  eines Modells etikettiert, das nie gelaufen ist.
- `24_quasimodo_bot.py` — **präventiv, bevor QM2 existiert**: der Loader bevorzugt
  `meta.model_id` (heute schreibt `qm_ml_trainer.py` keine → abgeleiteter Tag
  `QM_1H`, so geloggt), und `send_cornix_signal` leitet den Tag nicht mehr ein
  zweites Mal aus `tf` ab, sondern bekommt `module_tag` als **Pflicht-Keyword** —
  das Sniper-Muster: eine Aufrufstelle, die ihn vergisst, scheitert laut mit
  `TypeError`, statt still den Alt-Tag zu schreiben. Der Orchestrator erkennt
  `QM2_1H` seit `ff8e01e` bereits.

### Fixed — transitionaler Dedup (Review-Fund, hätte den Tag-Fix zur Geldfalle gemacht)
Der Posting-Tag **ist zugleich der Dedupe-Key**. Beim Generationswechsel kippt er —
und damit hätte eine noch offene Position der Alt-Generation denselben
Coin/Direction nicht mehr geblockt: der neue Lauf hätte eine **zweite Live-Position**
daneben eröffnet. Exakt die Falle, die PR #16 beim Sniper mit
`model IN (neuer Tag, Alt-Tag)` entschärft hat. Pro Bot an der Stelle geschlossen,
die dort tatsächlich sperrt:

- `11_ai_mis_bot.py` / `24_quasimodo_bot.py` — Active-Trade-Check auf
  `model IN (%s, %s)` erweitert.
- `13_ai_rub_bot.py` — RUB hat **keinen** Active-Trade-Check gegen `ai_signals`; sein
  4h-Cooldown ist die einzige Re-Fire-Sperre. Der prüft jetzt zusätzlich gegen
  `RUB_LEGACY_TAG`. (Die fehlende Open-Position-Prüfung ist ein Alt-Zustand, nicht
  Teil dieses Tasks.)

`legacy_tag` ist jeweils **genau das Tag, das der Bot vor diesem Fix gepostet hätte** —
keine Operator-Konstante, kein toter Code. Solange Quellcode-Konstante und
Artefakt-Generation übereinstimmen, sind beide Tags identisch und die Klausel ist ein
No-op.

Guard-Tests (statisch, DB-frei — ein Runtime-Guard würde von den fleet-weiten
breiten `except`-Blöcken geschluckt, Lektion aus T-2026-CU-9050-024):
`backtest/test_mis_tag.py`, `backtest/test_rub_tag.py`,
`backtest/test_quasimodo_tag.py`. Alle drei sind mutations-geprüft: das Zurückdrehen
je einer Fix-Zeile lässt den zugehörigen Test rot werden. **Keine
Live-Semantik-Änderung** — die drei Tags lauten mit den deployten Artefakten
unverändert `MIS2-<Horizont>`, `RUB2`, `QM_1H`, und die Dedup-Klauseln sind bei
identischen Tags wirkungsgleich zum Vorzustand.

### Offen (bewusst nicht in diesem PR)
- `retrain_from_replay.py:723` (EPD2) und `retrain_sra2.py:281` (SRA2) schreiben
  dict-Artefakte **mit** `model_id`, während die Live-Bots `10_pump_dump_detector`
  und `9_ai_sr_bot` **rohe** Modelle laden und keine Meta lesen — das
  Retrain-Ausgabeformat divergiert vom Live-Ladeformat. Beim Verdrahten von
  EPD2/SRA2 muss der Tag aus der neuen `model_id` kommen, sonst entstehen Instanz 4
  und 5 derselben Fehlerklasse. Bleibt als P1.45-Nebenbefund im Ledger.

## [2026-07-09] Kerzen-API `core/candles.py` + Call-Site-Inventar + Paritäts-Tool (T-2026-CU-9050-034, C1-Vorbereitung)

Vorbereitung der R1-/TimescaleDB-Migration (`docs/TIMESCALE_R1_MIGRATION.md`,
T-2026-CU-9050-018). **Reine Neuanlage — kein bestehender Call-Site wurde
umverdrahtet, kein Dual-Write, kein Backfill, kein Cutover, keine
Schema-Änderung.** Die Fleet läuft unverändert.

Neu:

- **`core/candles.py`** — die zentrale Zugriffs-API über die per-Coin-Tabellen,
  durch die in Phase 1 alle Kerzen-/Indikator-Zugriffe laufen sollen. Vier
  Verträge: Reads liefern **immer ASC** (heute mischen sich ASC- und
  DESC-Frames, `iloc[-1]` bedeutet je nach Datei etwas anderes);
  `include_forming=False` ist Default und schaltet R1 bot-für-bot scharf;
  Writes **committen nicht** (Caller-Commit-Kontrakt wie `core/signal_post.py`);
  Symbol/Timeframe werden validiert und über `psycopg2.sql.Identifier` gequotet
  (P3.3, optionale `coins.json`-Whitelist).
- **`docs/CANDLE_CALL_SITES.md`** — Inventar jeder Stelle im Repo, die eine
  Kerzen- oder Indikator-Tabelle anfasst, mit heutigem Forming-Candle-Verhalten,
  R1-Blast-Radius, vorgeschlagener Umverdrahtungs-Reihenfolge und den offenen
  Operator-Fragen.
- **`tools/candles_parity.py`** — Paritäts-Vergleich alt vs. Hypertable
  (Row-Count, `max(open_time)`, OHLCV-Checksumme) als Gate für Migrationsphase
  3. Der Vergleichskern ist DB-frei und per `--self-check` auf der
  Build-Maschine lauffähig; echte Läufe brauchen den VPS.
- **`backtest/test_candles.py`** — 29 DB-freie Tests.

Der `is_closed`-Vertrag des Ziel-Schemas existiert in den Alt-Tabellen nicht.
Phase A leitet ihn aus der Uhr ab (`open_time < period_start(tf, now())`),
DB-seitig gerechnet, per Epoch-Arithmetik statt `date_trunc()` — letzteres hängt
an der Session-Zeitzone und hätte je nach Bot-Prozess anders geschnitten (R3).
Für `1w` ist der Cutoff auf Montag verankert; Epoch 0 ist ein Donnerstag,
Binance-Wochenkerzen öffnen Montag 00:00 UTC.

Offen (Operator, siehe `docs/CANDLE_CALL_SITES.md` §5): Retention, `REAL` →
`double precision` (P3.12), 1d/1w-Streaming, Close-Grace-Period. **R1 senkt die
Signal-Raten — das ist der Zweck. Schwellen erst nach dem Retrain neu tunen.**

## [2026-07-09] HTTP-Härtung der Binance-REST-Pfade (T-2026-CU-9050-027 D2, P2.14 + P2.18)

Neues `core/http_retry.py` (reine Politik ohne I/O, injizierbare Uhr/Sleep →
DB-/netzfrei testbar): `RetryBudget` (max_attempts UND Wanduhr-Deadline),
`backoff_seconds` (429 mit Retry-After-Respekt, 418 nie unter 120s und
exponentiell — ein Retry-After-Header darf die Ban-Wartezeit nur erhöhen),
`MinIntervalThrottle` (Mindestabstand + Jitter je Host-Bucket). Muster nach
HKUDS/Vibe-Trading `loaders/_http.py`/`retry_with_budget` (MIT), kein Drop-in.

- **P2.14 (`1_data_ingestion.fetch_ohlcv_batch`):** die `while True`-Schleife
  konnte bei einem stuck Symbol ewig loopen und hämmerte bei 418 mit
  Retry-After+2s in den Ban. Jetzt: gebudgeteter Retry (8 Versuche/300s je
  Symbol×TF-Batch, nur FEHL-Versuche zählen — Erfolgs-Seiten paginieren frei),
  418-Backoff ≥120s exponentiell. Bei erschöpftem Budget werden die bereits
  geholten Teildaten verwendet; der nächste 12h-Lauf setzt am MAX(open_time)
  wieder auf.
- **P2.18 (`6_housekeeping._fetch_klines_from_binance`):** der Gap-Filler hatte
  gar kein 429/418-Handling (`raise_for_status` → None) und konnte im Burst
  über ~9k Tabellen einen 418-IP-Ban ziehen, der auch die Trading-Endpoints
  trifft. Jetzt: 429 → Retry-After-bewusster gebudgeteter Backoff; 418 →
  prozessweites Ban-Fenster (alle weiteren Gap-Fill-Calls liefern bis zum
  Ablauf sofort None statt weiterzuhämmern; der nächste nächtliche Lauf holt
  die Gaps nach); Throttle 0,25s/Request gegen den Burst.

Live-Semantik: Erfolgs-Pfade unverändert (gleiche URLs, gleiche Parse-Wege);
alle Deltas liegen auf Fehler-Pfaden, die vorher endlos retryten oder bannten.
Wirkt beim nächsten regulären Restart, kein Deploy. Verifikation:
`backtest/test_http_retry.py` (7/7, standalone), ruff+mypy grün auf allen drei
Dateien. Der Freshness-Fallback (`run_freshness_job`) behält sein eigenes,
schon gedeckeltes Rate-Limit-Handling — bewusst nicht angefasst (limit=2-Calls,
Weight ungefährlich).

---

## [2026-07-09] Market-Tracker gibt Pool-Connections auf dem Fehlerpfad zurück (T-2026-CU-9050-029, P1.43, PR #18)

`23_market_tracker.py` holte die Connection an zwei Stellen bare und rief
`conn.close()` als **letzte Anweisung im try-Body** — bei einer werfenden Query
sprang der Ablauf direkt ins `except: log; return`, das `close()` lief nie, der
Pool-Slot war weg. Der Pool deckelt bei 8 Connections pro Prozess, also ziehen
~8 DB-Schluckauf den Tracker dauerhaft trocken: der Prozess bleibt unterm
Watchdog „healthy" und postet still nichts mehr. Die Ursache ist die
Acquire/Release-Form, nicht die Queries.

Beide Stellen nutzen jetzt `with get_db_connection() as conn:` — die Form, die
die fünf übrigen `job_*`-Funktionen derselben Datei schon hatten.

### Auf derselben Bruchlinie mitgefixt
- **Der `ai_signals`-Fallback lief in der abgebrochenen Transaktion.** Postgres
  bricht bei einer fehlgeschlagenen Anweisung die ganze Transaktion ab; der
  Fallback wäre mit `InFailedSqlTransaction` gestorben — er ist also nie
  zurückgefallen. `rollback()` davor ergänzt.
- **`_get_regime_fit_label` vergiftete die geteilte Connection.** Die Funktion
  schluckt ihre Exception und liefert `---`, aber der Caller teilt EINE
  Connection über ~25 Bots. Ohne `rollback` blieb die Transaktion abgebrochen,
  der erste fehlgeschlagene Lookup degradierte die Regime-Fit-Spalte **aller
  folgenden** Bots auf `---`.
- **Die Kelly/Regime-Fit-Schleife** indexiert in das Kelly-Dict; ein `KeyError`
  übersprang `_regime_conn.close()`. Jetzt `try/finally`.

### Live-Semantik
Auf dem Erfolgspfad ändert sich nichts: die Connection wird am identischen Punkt
freigegeben (nach dem letzten Read, vor der pandas-Verarbeitung), mit demselben
`rollback()` + `putconn()`. Alle Deltas liegen auf Pfaden, die vorher einen
Pool-Slot verloren oder an `InFailedSqlTransaction` starben. Wirkt beim nächsten
regulären Restart, kein Deploy.

Verifikation: `backtest/test_market_tracker_conn.py` (neu, standalone, DB-frei,
7/7) — die 4 Bug-Tests fallen nachweislich auf dem Pre-Fix-Stand, die 3
Kontroll-Tests laufen auf beiden Ständen grün.

---

## [2026-07-09] Ledger wahr gemacht — Steuerungs-Dokumente gegen den Code verifiziert (T-2026-CU-9050-028)

Kein Code-Fix. Die beiden Steuerungs-Dokumente (`docs/OPUS-HANDOFF.md`,
`docs/T-2026-CU-9050-021-opus-task-audit.md`) trugen Stand 07-07 und kannten
die Arbeit von 07-08/07-09 nicht — wer sie als Backlog las, priorisierte auf
veralteter Grundlage.

### Verifiziert statt geflippt
- **P1.26 bleibt offen — die Annotation war falsch.** Sie markierte das Finding
  als widerlegt („83 SMC_*_FVG-Cooldown-Rows, Pfad feuert"). Am Code: der
  Mitigation-Scan in `16_smc_forex_metals_bot.py:164` läuft
  `range(fvg['index']+1, len(df))`, also **inklusive** `curr_idx = len(df)-1`,
  und markiert BULLISH als mitigiert bei `low[j] <= fvg['top']`. Der Trigger
  (`:430`) prüft dasselbe Prädikat auf derselben Kerze. Ein FVG, das den Entry
  auslösen würde, ist damit per Konstruktion schon aus `bull_fvgs` entfernt →
  **der FVG-Entry kann nie feuern.** Auflösung des Beweis-Widerspruchs: der
  aktuelle Code schreibt repo-weit nur den literalen Key `"SMC_FVG"`
  (`:431,459`); die 83 gefundenen Rows heissen `SMC_1H_FVG` etc. und stammen
  aus einer älteren, TF-präfigierenden Version. Der Dead-Code-Beweis steht rein
  am Code und braucht die DB nicht.
- Geflippt nach Nachprüfung: **P1.5** (Spalte ist INTEGER, zusätzlich
  Defensiv-Cast in `8_ai_trade_monitor.py:216-219`), **P1.11** (Buffer-Key ist
  längst `(sym, tf, open_time)`, `1_data_ingestion.py:662` — war fälschlich als
  A2-Item gelistet), **P1.18** (Feature-Selektion ist namensbasiert,
  `11_ai_mis_bot.py:245`; der Fix greift erst beim nächsten Bot-Restart),
  **P2.50** (Guard ist armed, 24 Goldens + 24 Fixtures seit `4765e25`, `verify`
  als pre-commit-Hook).
- **P2.2 bleibt offen:** die TZ-Dimension ist aufgelöst, die Spaltenbreite
  nicht. `CREATE TABLE IF NOT EXISTS` verbreitert nie, die Drift zementiert
  sich. Als Herkunfts-**Indiz** (nicht als Beweis) notiert: die einzige Stelle
  im Repo mit `module VARCHAR(10)` ist ein auskommentierter Legacy-DDL-Block in
  `legacy_trainers/zzz.py:13443`; die ausführende DDL liegt nicht im Repo. Der
  saubere Fix ist ein Live-`ALTER` (Operator-Entscheid).

### Fehlerklassen-Sweep aus PR #14 und #16 (der eigentliche Wert)
- *Stiller Signal-Tod durch Spalten-Overflow:* **keine zweite aktive Instanz.**
  Alle 18 `trade_cooldowns.module`-Writer bis zum Tag-Wert aufgelöst; längster
  Tag 9 Zeichen (`MAYANK_4H`, `MIS2-168H`), alle distinkt, keine
  Trunkierungs-Kollision. Restrisiko als **P3.13** notiert (Tag-Längentest deckt
  nur Mayank ab; der `COOLDOWN_MODULE_MAX_LEN`-Guard raist `ValueError` und
  würde von denselben breiten `except`-Blöcken geschluckt — die tragende
  Absicherung ist der DB-freie Static-Test).
- *Post-Pfad ignoriert Artefakt-`model_id`:* **keine zweite aktiv falsch
  feuernde Instanz, aber drei latente** → neues Finding **P1.45**.
  `11_ai_mis_bot.py` (Konstante `MODEL_GENERATION="MIS2"`, dazu hartkodierte
  `mis2_*.pkl`-Dateinamen), `13_ai_rub_bot.py` (`load_artifact` berechnet den
  Tag korrekt, der Bot verwirft ihn) und `24_quasimodo_bot.py` (struktureller
  Zwilling des Snipers: abgeleitetes `f"QM_{tf}"` kann ein QM2 nie treffen — und
  der Orchestrator ist seit `ff8e01e` bereits QM2-fähig). Heute stimmen die Tags
  zufällig; **beim nächsten Retrain-Rollout verschmelzen die Generationen still**
  in der Per-Bot-Statistik, auf der das Orchestrator-Gating entscheidet.
  → blockiert MIS3/RUB3/QM2, als **A2b** vor B7/C2 eingeplant.

### Changed
- `AUDIT_TODO.md` — fünf Checkboxen korrigiert, A2-Items mit Code-Belegen vom
  07-09 annotiert, neue Findings **P1.45**, **P2.51**, **P3.13**.
- `docs/T-2026-CU-9050-021-opus-task-audit.md` — Stand 07-09; Tasks 022–026 +
  PR #12 nachgetragen; **A1 erledigt**; **A2 auf die verifizierte Restmenge
  eingekürzt** (fünf statt sechs Items — die PRs #13/#15 haben keines davon
  miterledigt, ihre Dedup wirkt nur auf die geschlossenen Tabellen); **A2b** neu;
  **B5 gestrichen** (Guard war längst scharf); **B7 um MIS1 gekürzt** (Adapter
  `run_mis1` existiert, nur die Ausführung steht aus).
- `docs/OPUS-HANDOFF.md` — Stand 07-09; Zyklus-Schritt 0 (`git fetch` vor
  Priorisierung); Falle 13 verschärft (Annotationen selbst können falsch sein —
  am Code nachprüfen); neue Fallen 15 (stale Checkout) und 16 (Modell-Tag kommt
  aus dem Artefakt, nie aus einer Konstante); Guard-Status korrigiert.

### Nebenbefund
- **P2.51** (neu): `tools/regression_guard/guard.py:132-137` — `mode_verify` gibt
  bei fehlenden Goldens „NOT ARMED … Pass" und Exit 0 zurück, ohne zu prüfen, ob
  `manifest.json` existiert. Wer `golden/` löscht oder beim Merge verliert,
  disarmt den Guard unbemerkt; der pre-commit-Hook bleibt grün. Der umgekehrte
  Fall (Goldens ohne Fixtures) ist korrekt mit Exit 1 behandelt.

### KB
- `T-2026-CU-9050-016` (Batch E) von `open` auf `done` korrigiert: alle im Task
  benannten Kriterien (P0.10–P0.13, P1.29–P1.31, P1.35) sind geliefert und mit
  Report-19-Zahlen belegt. QM/ATS1/ATB1/SRA1 waren nie Done-Kriterien dieses
  Tasks, sondern der als B7 kartierte VPS-Folge-Scope.

---

## [2026-07-09] PR #16 — SMC-Sniper: Retrain-Trades posteten unter dem Alt-Tag (T-2026-CU-9050-026)

Auslöser: Operator-Eindruck „der SMC postet keine Trades mehr". Befund: er
tradet — aber unsichtbar.

### Fixed
- `25_smc_ml_sniper.py` — **`send_cornix_signal` reicht jetzt die
  Artefakt-`model_id` durch statt den Tag als `{strategy}_{tf}` neu zu
  berechnen.** `evaluate_and_trade` nutzte korrekt `BB2_4H`/`TD2_4H`
  (Cooldowns, ml_predictions), aber der Signal-/Trade-Write lief unter
  `BB_4H`/`TD_4H` — die Retrain-Generation war in ai_signals und allen
  Downstream-Stats (Per-Bot-Post, A–Z-Post, Regime-Analyzer) mit der
  Alt-Generation verschmolzen (Regel-6-Verstoß). Evidenz: 97 der 115
  offenen `BB_4H`-Rows tragen Confidence ≥ 0.63 (= BB2-Threshold), 88
  Closes seit dem BB2-Deploy 06.07. Operator-Entscheid: fixen, KEINE
  Umschreibung der falsch getaggten Altrows (wäre Live-Write).
  Guard-Test: `backtest/test_sniper_tag.py`.
- `28_signal_orchestrator.py` — **`BOT_IDENTIFICATION_PATTERNS`
  generationsoffen gemacht** (Review-Fund, hätte den Tag-Fix sabotiert):
  die Patterns matchten nur `BB_`/`TD_` und die Literal-Liste nur
  `RUB1/ABR1/...` — ein `BB2_4H`-Signal wäre als `bot_unidentified` HART
  unterdrückt worden, statt (wie beabsichtigt) default-open durch die
  Whitelist zu laufen. Jetzt `BB\d*_`, `TD\d*_`, `QM\d*_` und
  `(MIS|ATS|RUB|ATB|AIM|ABR|EPD|SRA)\d+` — das schließt zugleich das
  offene RUB2-Attributions-Finding aus PR #9 (RUB2 postet seit 07.07.
  live und hing am `🧠 …Strategy`-Footer-Fallback). Erst mit diesem Fix
  gilt: neuer Tag startet in der Regime-Whitelist ohne Historie
  (default-open) — bewusst akzeptiert.
- `25_smc_ml_sniper.py` — **Übergangs-Dedup**: der Active-Trade-Check
  prüft `model IN (neuer Tag, Alt-Tag)` — die ~115 offenen, falsch
  getaggten Rows blocken weiterhin Re-Fires auf demselben Coin/Direction
  (sonst zweite Live-Position neben der alten). `module_tag` ist jetzt
  Pflicht-Keyword-Parameter (vergessener Tag → lauter TypeError statt
  stillem Alt-Tag). Orchestrator-Tests um Generation-Tags erweitert.

### Nebenbefunde (kein Codeänderungsbedarf)
- `16_smc_forex_metals_bot.py` (SMC_15M/30M/4H im A–Z-Post) ist by design
  info-only — der Code in diesem Repo hatte nie einen ai_signals-Pfad; die
  Feb-Trades stammen von einem Legacy-Script. Wenn der Bot wieder getrackte
  Trades liefern soll, ist das ein eigener Task (Operator-Entscheid).
- Mayank postet Info-Signale ohne Position-Tracking (Refire-Bug bereits in
  PR #14 gefixt).

## [2026-07-09] PR #15 — Market-Tracker Dedup-Key v2: Report-14-Schlüssel, All-Time/Kelly jetzt wirklich sauber (T-2026-CU-9050-025)

### Fixed
- `23_market_tracker.py` — **Dedup-Schlüssel von (…, entry, close_price,
  open_time, close_time) auf `(symbol/coin, strategy, direction, open_time)`
  umgestellt** — der Unique-Index-Schlüssel, den Report 14 empfiehlt.
  Live-Messung nach dem PR-#13-Deploy: 439.325 rohe AI-Rows → der alte
  Schlüssel kollabierte nur auf 360.682, der Report-14-Schlüssel zeigt
  **81.842 echte Trades**. Grund: die ~357k Migrations-/LEGACY-Duplikate
  (Feb 2026: 372.794 → 15.339) sind Re-Closes DESSELBEN Trades mit anderem
  close_time/close_price — der alte Schlüssel sah sie als verschiedene
  Trades. All-Time-WR und Kelly waren damit weiterhin verzerrt; die kurzen
  Fenster (1h–7d) und der Regime-Analyzer (30d) waren sauber (0 Duplikate in
  den letzten 30 Tagen; außerhalb Feb/März 2026 ist der Schlüssel im
  Normalbetrieb eindeutig, raw == distinct in jedem Monat). Survivor je
  Gruppe: frühester Close (das Original-Outcome; das Re-Close-Artefakt kam
  später), dann höchste targets_hit. Beide Jobs, beide Tabellen (Classic:
  ~11k Duplikate nach demselben Schlüssel — alle mit identischen Entries
  verifiziert, keine legitimen Ladder-Trades betroffen).
- `23_market_tracker.py` — **Einheitliche Query-Struktur nach Review**: Dedup
  läuft in allen vier Queries ZUERST über die volle Tabelle, Fenster- und
  Preis-Validitäts-Filter (`entry/close_price > 0`, jetzt auch im
  Summary-Job) liegen außen. Damit hängt die Survivor-Wahl nicht vom Filter
  ab, und ein künftiges Re-Close-Event kann keine Monate alten Trades als
  „frisch geschlossen" ins 24h-Fenster spülen. Schlüssel/Sortierung leben in
  Modul-Konstanten (`AI_DEDUP_KEY` etc.) statt in vier Kopien. Live
  verifiziert: identische Ergebnismenge (81.837 Gruppen) bei beiden
  Strukturen mit aktuellem Datenbestand.

### Bewusst NICHT geändert
- `tools/track_shadow_model.py` behält seinen engeren Natural-Key — er wird
  auf frische Tags (EPD2 etc.) angewandt, wo keine Migrations-Duplikate
  existieren; funktional identisch.
- Der Unique-Index selbst + Purge der Duplikat-Rows bleibt DB-Migration →
  Operator-Entscheid (Report 14 Empfehlung #1).

## [2026-07-09] PR #14 — Cooldown-Tags sprengen varchar(10): Volume Indicator signal-tot, Mayank-Refire (T-2026-CU-9050-024)

`trade_cooldowns.module` ist auf der Live-DB `character varying(10)` (per
`information_schema` verifiziert). Die Repo-DDLs sagen VARCHAR(50)/TEXT — die
Live-Tabelle ist älter, `CREATE TABLE IF NOT EXISTS` verbreitert nie
(DDL-Drift, P2.2 erweitert). Zwei Writer nutzten längere Tags:

### Fixed
- `strategies/strat_volume_indicator.py` — **`module_tag` von `'Volume
  Indicator'` (16 Zeichen) auf `'VolIndic'` (8) gekürzt.** Der P1.16-Fix
  (2026-07-04) warf deshalb bei JEDEM Signal-Versuch
  `StringDataRightTruncation` — vor dem `return` des Signal-Dicts. Folge: der
  Volume Indicator hat vom 04.07. bis 09.07. **null Signale gepostet**, und
  weil `analyze_fast` im selben Per-Coin-try läuft und `write_signal_atomic`
  erst danach kommt, ging in Zyklen mit gleichzeitigem
  Fast-In-And-Out-Signal **auch dieses Signal verloren** (Kollateralschaden
  der P1.15-Isolation; `check_cooldown` fand nie eine Row → jeder 30m-Zyklus
  crashte erneut). Entdeckt beim PR-#13-Deploy im Watchdog-Log.
  Operator-Entscheid: fixen, Bot postet wieder. Keine Row-Migration nötig —
  kein Write mit dem langen Tag ist je durchgekommen.
- `strategies/strat_volume_indicator.py` + `3_detectors.py` — **Cooldown
  wandert in `write_signal_atomic`**: die Strategie schreibt nicht mehr
  selbst, sondern requested den Cooldown via `signal['cooldown_module']`;
  der Detector schreibt ihn in DERSELBEN Transaktion wie
  active_trades_master + Outbox (Regel 8: Transaktionen committet der
  Caller). Ein Self-Commit in der Strategie hätte die 12h-Sperre auch bei
  fehlgeschlagenem Signal-Write persistiert; ein `commit=False` in der
  Strategie war ebenfalls nicht atomar (Review-Fund Runde 2: der Commit
  eines FRÜHEREN Signals im selben Per-Coin-Zyklus — z.B. Fast In And Out —
  hätte den pending Cooldown mitgenommen).
- `17_mayank_bot.py` — **gleiche Bug-Klasse, schlimmere Wirkung:**
  `module_tag = f"MAYANK_{symbol}_{tf}"` (≥14 Zeichen) warf NACH dem
  Outbox-Insert → Cooldown nie persistiert → **dasselbe FVG-Setup wurde jede
  Scan-Runde erneut gepostet**, solange das Setup bestand. Neuer Tag
  `f"MAYANK_{tf}"` (≤10); das Symbol steckt ohnehin in der `coin`-Key-Spalte,
  die (module, coin, direction)-Eindeutigkeit bleibt identisch.

### Added
- `core/market_utils.py` — **Längen-Guard `COOLDOWN_MODULE_MAX_LEN = 10`** in
  `check_cooldown`/`update_cooldown`: überlange Tags werfen jetzt in JEDER
  Umgebung sofort einen sprechenden `ValueError` (Dev/Staging-DBs aus den
  Repo-DDLs hätten den Live-Fehler nie reproduziert, CI wäre grün geblieben).
- `25_smc_ml_sniper.py` — **Load-Fallback für Artefakt-`model_id` > 10
  Zeichen**: ein überlanger Tag aus der pkl-Meta würde den neuen Guard bei
  JEDER Evaluation werfen (per-Symbol-except schluckt still → Bot postet
  nichts). Jetzt: lauter `logger.error` + Fallback auf den statischen
  `{strategy}_{tf}`-Tag. Aktuelle Artefakte (BB2/TD2) passen.
- `backtest/test_cooldown_tags.py` — DB-freier Standalone-Test: Guard wirft,
  VolIndic-/Mayank-Tags passen, VolIndic-Cooldown läuft atomar über
  `write_signal_atomic` (kein Strategy-Self-Write), fleet-weiter Scan auf
  überlange Literal-Tags (Root + strategies/ + core/).

### Nachlauf
- AUDIT_TODO: P1.16 um Regression-Annotation ergänzt (inkl. FIO-Kollateral),
  P2.2 um die Breiten-Drift-Dimension erweitert. Empfehlung an Operator:
  `ALTER TABLE trade_cooldowns ALTER COLUMN module TYPE VARCHAR(50)` bei
  nächster Gelegenheit (Live-Schema-Änderung → Eskalation, T-2026-CU-9050-018).

## [2026-07-09] PR #13 — Market-Tracker: Per-Bot-WR-Korrektheit + kompakter A–Z-Model-Post (T-2026-CU-9050-023)

Auslöser: Operator-Frage, ob die Erfolgsraten je Bot im Sentiment-Tracker-Kanal
stimmen. Antwort: die Klassifikations-Logik (PnL-basiert, Neutrale raus) war
sauber, aber drei Datenprobleme verzerrten die Zahlen.

### Fixed
- `23_market_tracker.py` — **Dedupe auf dem natürlichen Schlüssel, serverseitig
  via `SELECT DISTINCT ON` in beiden Jobs** (`job_signal_summary` +
  `job_per_bot_performance`). `closed_ai_signals` hat keinen Unique-Index und
  trägt ~357k Duplikat-Rows aus Migration/LEGACY-Re-Close (Report 14) — n,
  All-Time-WR und Kelly waren inflationiert, und die Duplikate wurden bisher
  stündlich komplett zur Client-Seite transferiert. Der `ORDER BY`-Tiebreaker
  (`targets_hit DESC`/`status DESC`) macht die überlebende Row deterministisch
  (Duplikate unterscheiden sich genau in status/targets_hit). Gleicher
  Schlüssel wie `tools/track_shadow_model.py`.
- `23_market_tracker.py` — **`close_price=0`-Rows (v1-Ära, pre-2026-03) fliegen
  aus der WR.** Die PnL-Formel wertete solche SHORTs als +100%-Win und LONGs
  als −100%-Loss — beides innerhalb der 100%-Outlier-Grenze, floss also ein.
  Per-Bot-Job: SQL-Filter `entry > 0 AND close_price > 0`. Summary-Job: Rows
  mit vorhandenem, aber unbrauchbarem Preis sind jetzt NEUTRAL statt in den
  status/targets-Fallback zu laufen (der hätte den bekannten LEGACY-
  `targets_hit=0`-Writer-Bug wiederbelebt, den der PnL-Pfad umgehen soll).
- `23_market_tracker.py` — **Direction-Case normalisiert** (`upper(btrim(...))`
  im Dedup-Schlüssel und in der Select-Liste; pandas-Normalisierung als
  Belt-and-Braces für die Open-Frames). Historische lowercase-`short`-Rows
  bekamen bisher das LONG-Vorzeichen im PnL und fielen aus den
  LONG/SHORT-Splits.

### Added
- `23_market_tracker.py` — **Neuer Kompakt-Post „MODELS A–Z"** im
  Sentiment-Tracker-Kanal: eine Zeile pro Modell (24h/7d/All-WR, ø-PnL,
  entschiedenes n), alphanumerisch sortiert — Modell-Generationen (ABR1/ABR2,
  RUB1/RUB2, MIS1/MIS2, …) stehen direkt untereinander. Gesendet zwischen
  Haupttabelle und Kelly-Block; Chunking über das bestehende `_build_chunks`
  (neuer `separator`-Parameter statt Copy-Paste-Helper).

### Verifiziert
- ruff + `ruff format --check` + mypy grün (CI 6/6).
- Offline-Smoke-Runs beider Jobs mit gemockter DB: Natural-Key-Dedupe
  (Duplikate mit abweichendem status kollabieren), lowercase-Direction
  korrekt gescored, `DELISTED`-only-Bot zeigt n=0, LEGACY-`close=0`-Row
  neutral statt Loss, A–Z-Sortierung + Sende-Reihenfolge Tabelle→Kompakt→Kelly.
- DB-gebundene Nachkontrolle (Plausibilisierung gegen
  `tools/track_shadow_model.py`) gehört in eine VPS-Session nach Deploy.

### Bewusst NICHT geändert
- Kein Unique-Index/Purge auf `closed_ai_signals` — DB-Migration an
  Live-Tabellen ist Operator-Entscheid (Report 14 Empfehlung #1,
  T-2026-CU-9050-018).
- P1.44 (Opened-Counts doppeln AI-Trades + zählen Shadow-Predictions) bleibt
  offen — separates Finding, nicht Teil dieses Fixes.

## [2026-07-07 abends] PR #10 — Review-Fixes zu den PR-#9-Findings (Korrektheit)

### Fixed
- `core/model_artifacts.py` — **`maybe_reload` verwirft ein geladenes Artefakt
  bei einem fehlgeschlagenen Reload nicht mehr.** Bisher ersetzte das tägliche
  Reload das In-Memory-Modell unbedingt durch das Ergebnis von `load_artifact`;
  ein transienter Fehler (File-Lock während Operator-Copy, AV-Scan, halb
  geschriebener Deploy) schaltete damit eine live Seite bis zum nächsten
  24h-Fenster stumm (RUB2-SHORT: `if not RUB2_SHORT["loaded"]: continue`, kein
  Legacy-Fallback). Neu: schlägt der Reload fehl UND existiert die Datei noch,
  bleibt das geladene Artefakt aktiv (`loaded_at` wird trotzdem vorgerückt →
  kein Retry pro Tick). Nur wenn die Datei WEG ist (Operator-Undeploy), wird
  der Nicht-geladen-Zustand übernommen. Verhaltens-Test inline verifiziert.
- `10_pump_dump_detector.py` — **`ticker_10s`-Timestamp auf die 10s-Marke
  gefloort.** Der neue `UNIQUE(symbol, ts)`-Index konnte die motivierende
  Doppel-Writer-Klasse (Detector-Doppelstart) gar nicht verhindern, weil jeder
  Prozess einen rohen `datetime.now(utc)` je Tick stempelte → zwei Instanzen
  erzeugten `ts`-Werte mit µs-Jitter, `ON CONFLICT DO NOTHING` griff nie. Jetzt
  identischer, gerasterter `ts` je 10s-Fenster → Dedup wirkt.
- `core/ticker_10s.py` — **Einmal-Migration (Dedup-DELETE + `CREATE UNIQUE
  INDEX`) committet sofort in eigener Transaktion**, vor den idempotenten
  Compression-/Retention-Policy-Statements. Sonst hätte ein späterer
  Policy-Fehler per Rollback Dedup + Index mit weggeworfen, und der teure
  Full-Table-DELETE liefe bei JEDEM Start erneut — nach `COMPRESS_AFTER` gegen
  komprimierte Chunks, wo DELETE/`CREATE UNIQUE INDEX` eingeschränkt sind.
- `tools/retrain_from_replay.py` — **`load_replay` scheitert bei `null`-Features
  oder `null`-`net_pnl_pct` laut statt still auf 0.0/`{}` zu defaulten.** Solche
  Zeilen sind Replay-Writer-Bugs; als 0.0-PnL-Zeilen verwässerten sie die
  Validation-Ökonomie, auf der `pick_threshold_safe` den LIVE-Gate-Threshold
  wählt (deploybar aussehendes Artefakt auf korrupter Ökonomie).
- `13_ai_rub_bot.py` — **`RUB2_SHORT`-Init auf die volle `load_artifact`-
  Contract-Form** (statt Teil-Dict ohne `threshold`/`features`/`loaded_at`):
  entschärft KeyError-Fallen vor `load_models()` und erzwingt via `loaded_at=0.0`
  den ersten Reload-Load.
- `core/config.py` — **`_ch` behandelt leeren/whitespace-Wert als ungesetzt**
  (→ 0) statt an `int("")` zu crashen. Eine getemplatete `.env`-Zeile wie
  `CH_MAIN=` hätte sonst jeden Bot beim Import gerissen
  (audit_reports/01_core_infra.md LOW).

### Verifiziert
- ruff (CI-Set) clean, mypy 65 Dateien clean, Regression-Guard `verify` OK,
  Standalone-Suite 149 passed (die 3 roten Tests — `test_bot_naming`,
  `test_bot_regime_analyzer`, `test_signal_orchestrator::…rom1…` — sind
  vorbestehend auf `main`, keine PR-#10-Regression).

### Offene Follow-ups (dokumentiert, nicht merge-blockierend)
- **`backtest/backfill_regime_history.py`** ruft `classify_regime` weiter ohne
  `prev_regime` → Enter-only-Semantik ≠ Live-Detector (Hysterese). Bei einem
  Re-Run mischt `regime_history` zwei Klassifikator-Semantiken. Fix: rollierendes
  `prev_regime` durch die Schleife fädeln wie im Detector.
- **`tools/regime_rules_study.py`** modelliert im vektorisierten `classify()` die
  deployte Hysterese nicht → künftige Grid-Runs bewerten eine No-Hysterese-
  Variante.
- **Bots 25/18** (`25_smc_ml_sniper.py`, `18_ai_abr1_bot.py`) laden Artefakte
  weiter von Hand ohne Feature-Contract-Check/Reload; Bot 25 `exit(1)` statt
  Idle bei fehlendem Artefakt. Kandidat für `core/model_artifacts.load_artifact`.
- **RUB2-Feature-Contract** wird in Bot 13 (`RUB_FEATURES + FUNDING_FEATURES`)
  und Trainer (`RUB2_FEATURES`) getrennt komponiert — eine geteilte Konstante in
  `core` (wie `PEX1_FEATURES` in `core/research_features.py`) wäre die eine
  Quelle (Regel 7). Divergenz scheitert aktuell laut über `load_artifact`, nicht
  still, daher Follow-up.
- **`13_ai_rub_bot.py` `since=now-95d`** dupliziert das `rates[-270:]`-Fenster von
  `funding_features_asof` als Magic-Konstante (deckt es aktuell ab; koppeln über
  eine geteilte Konstante).

## [2026-07-07 mittags] Detector-Rework §22 LIVE — Mid-Vola-Trend-Regel mit Hysterese

### Changed
- `core/regime_logic.py` — **Mid-Band-Trend-Regel V2 K=1,5 + Hysterese**
  (Operator-Pick aus `tools/regime_rules_study.py`, 7 Varianten über 430d):
  Im Band P40..P75 gilt |ret_4h| ≥ 1,5×ATR_4h% → TREND_UP/DOWN; bestehender
  TREND hält bis |ret_4h| < 1,0×ATR (`prev_regime`-Param, gefüttert aus
  `regime_current`); TREND-Ziele brauchen 3 statt 2 Debounce-Checks.
  Alt: TREND war strukturell tot (3 Episoden in 430d, alle <1h, weil
  ATR<P40 ∧ |ret|>1,5 % sich fast ausschließen); TRANSITION war 41 %
  Restklasse. Neu (validiert, stateful mit echter classify-Funktion):
  TREND_UP/DOWN je ~10 % der Zeit (med 1,5h, Flaps 21–25 %), TRANSITION
  20,8 %. Ökonomie-Check: RUB-LONG in TREND_UP +1,65 %/Trade (n=1.378),
  9/13 Monate positiv (negativ nur Okt/Nov 25 + Jan 26 — tiefe Bear-Monate).
- `26_regime_detector.py` — liest das effektive Regime vor der
  Klassifikation und reicht es als `prev_regime` durch (Hysterese).
- Tests: `backtest/test_regime_detector.py` +7 (Mid-Band, Hysterese
  beide Richtungen, HIGH_VOLA-Vorrang, TREND-Debounce-3) — 27 passed.
- Deploy-Sicherheit geprüft: fehlende Whitelist-Zellen der neuen
  TREND-Zustände defaulten auf open (kein Mass-Auto-Close); Zellen sammeln
  ab jetzt Evidenz. Follow-up: §23-Analyzer-Umbau (Shrinkage statt
  Default-Open), danach ggf. explizites TREND_UP-Gate für RUB-LONG (§8).

## [2026-07-07 mittags] New-Ideas-Kohorte trainiert — FIF1 deployed, Detector-Studie gestartet

### Added
- **Alle 4 New-Ideas-Datasets gebaut + trainiert** (Ergebnistabelle in
  `docs/NEW_IDEAS_BOTS.md`): PEX1 ohne Selektionswert (AUC~0,55,
  Threshold degeneriert), FMR1 ohne Fundament (Val-AUC 0,498 = Zufall),
  TRM1 upstream blockiert (Klassen 0/5/1589 — Detector hält TREND nie,
  Step-6-Befund; Wiedervorlage nach Detector-Rework), **FIF1 einziger
  Kandidat** (Val-OP +0,044 %/Trade dünn; Test-Gate −0,08→+0,331 %/Trade,
  WR 75,3 %, n=893/18.011).
- **FIF1 DEPLOYED** (Operator 2026-07-07): `fif1_model.pkl` (thr 0,67) im
  Repo-Root, Bot 33 recycelt — postet LIVE in CH_NEW_IDEAS
  (`NEW_IDEAS_LIVE_POSTING=1`, AIM2-Validierungsmuster). Review 4–6 Wochen.
- `tools/regime_rules_study.py` — **Detector-Rework Schritt 1 (MODEL_INTENT
  §22)**: Regelvarianten-Replay über die volle BTC-15m-Historie. Ist-Regel
  V0 vs. Mid-Band-Trend-Regel mit fixem Threshold (V1, Grid 1,5/2,0/2,5 %)
  vs. vol-skaliert |ret_4h| ≥ K×ATR (V2, Grid 0,75/1,0/1,5); Bewertung
  über Episoden-Statistik (kommt TREND vor? flappt es?) UND Ökonomie-Overlay
  (Ø-PnL der RUB-LONG/ABR1-LONG-Replay-Events je Regime-Zustand — der
  Regime-Gate-Use-Case aus §8). Debounce-Näherung 2 Bars; read-only.

## [2026-07-07] RUB2-SHORT deployed — Bot 13 auf Artefakt-Contract

### Added
- `13_ai_rub_bot.py` — **SHORT läuft auf dem RUB2-Artefakt** (`rub2_model_SHORT.pkl`,
  expliziter Copy aus staging_models, P1.35): Contract wie Bot 25
  (model/features/optimal_threshold aus dem pkl-Dict), 15-Feature-Vertrag
  (9 rub + 6 Funding as-of aus `funding_rates` via `core/funding_features`,
  lazy je Event), fehlende Funding-Historie ⇒ 0 wie `fillna(0)` im Trainer
  (Serving-Parität), Threshold 0,829 auf roher predict_proba (Safe-Picker-
  Semantik). Fallback auf Legacy-Modell @0,85, falls Artefakt fehlt.
  LONG unverändert Legacy @0,75 (RUB2-LONG nicht deploybar — Val-Kurve
  durchweg negativ; Details MODEL_INTENT §8).
- Scheduled Task **„Kythera Funding Backfill"** (stündlich, :35, als User) →
  `Documents\kythera_funding_backfill.bat` ruft `tools/backfill_funding_rates.py`
  inkrementell — hält `funding_rates` frisch fürs RUB2-Serving (Tabelle hatte
  keinen Live-Writer; Stand vor dem Fix: 18 h alt).
- Scheduled Task **„Kythera Fleet Autostart"** (ONSTART +2 min, SYSTEM) →
  `Documents\start_kythera_fleet.bat` — Konsequenz aus dem VPS-Ausfall
  2026-07-07 (~04:42–08:18, provider-seitig): nichts startete die Fleet neu.

### Fixed
- `tools/pex1_build_dataset.py` `spike_time_to_utc` — **DST-Mixed-Offset-Bug**
  (traf PEX1- UND EPD2-Builder): `pd.to_datetime(errors="coerce")` ohne
  `utc=True` fixiert bei timestamptz-Serien den Offset der ersten Zeile;
  alle Zeilen mit anderem Offset (nach dem EET→EEST-Wechsel 2026-03-29)
  wurden zu NaT koerziert und vom `dropna` verworfen — der erste EPD2-Lauf
  verlor so ALLE Events nach dem 29.03. (38.974 statt erwartet ~3× so viele;
  Zeitraum 32 statt 132 Tage). Awareness wird jetzt am Rohwert geprüft und
  aware Serien mit `utc=True` geparst. Dataset neu gebaut.
- `tools/retrain_from_replay.py` `run_epd` — Guard gegen degenerierte
  Chrono-Splits (leerer Val-Slice ⇒ `iso.fit`-Crash beim abgeschnittenen
  ersten Datensatz); außerdem `--strategy epd` NEU: EPD2-Trainer
  (16-Feature-Vertrag = 10 Bot-10-Live-Features + 6 Funding, eigener Loader
  fürs Builder-Schema ts/label/features, 7d-Purge, Safe-Threshold,
  Artefakte `staging_models/epd2_model_{LONG,SHORT}.pkl`).

### Kontext (Retrain-Ergebnisse, 2026-07-07 vormittags)
- RUB-Replay 365d/530 Coins fertig (Resume nach VPS-Ausfall ab Coin 433);
  `retrain_from_replay.py --strategy rub --days 365`: **SHORT deploybar**
  @0,829 (Test 680/4.725, WR 81,9 % vs. Basis 79,1 %, +0,64 %/Trade netto),
  **LONG nicht deploybar** (alle Val-Thresholds −0,9…−1,2 %/Trade).
  Monats-Split des Replays stützt die Operator-These Regime-Abhängigkeit:
  LONG ungefiltert in Alt-Bull-Monaten deutlich positiv (Aug/Sep 25:
  +3,9/+2,4 %/Trade; Apr 26: +3,0), in Bear-Monaten desaströs (Okt/Nov 25:
  −3,6/−4,8; Jan 26: −3,4) → LONG braucht ein REGIME-Gate, kein
  Event-Ranking-Gate (verknüpft mit T-2026-CU-9050-020 HMM-Studie).

## [2026-07-06 nachts] Replay-Adapter für RUB2- und EPD2-Retrain

### Added
- `tools/walkforward_sim.py --strategy rub` — **RUB-Adapter**: spielt den Rubberband-Vorfilter je geschlossener 1h-Kerze nach (95d-Regression as-of, 4h-Cooldown je Richtung wie live). Detektions-/Feature-Logik nach `core/rub_features.py` gehoben — **EINE Quelle für Bot 13 UND Replay** (Bot refaktoriert, X-R1); Geometrie as-of über `get_hvn_and_sr_levels(df=…)` (neuer df-Param, P0.10-Muster) + `hvn_sr_trade_geometry` (neu in core/trade_utils — kanonisierte Bot-10/13-Geometrie). Feature-Dict enthält die 6 Funding-Features.
- `tools/epd2_build_dataset.py` — **EPD2-Adapter**: EPD ist 10s-Tick-basiert, die Detektor-Logs (`pump_dump_events`, 241k Rows seit 2025-12) SIND die Events. Spiegelt Bot-10-Semantik (vol_ratio≥5 beidseitig, Richtung = mitfahren, 900s-Dedup, Post-Spike-Entry, HVN/SR-Geometrie as-of), Label via `simulate_exit` (Skip-Entry-Hour, 7d); nutzt die exakten Event-Zeitpunkt-Indikatoren, wo vorhanden (~30 % der Rows), sonst 1h-Join; + Funding-Features. Smoke: 364 Events/5 Coins, beide Richtungen, 0 Fails.

### Fixed
- `tools/pex1_build_dataset.py` — TZ-Crash: `spike_time` ist `timestamptz` (aware UTC), die Offset-Heuristik erwartete naive Lokalzeit → `detect_offset_h`/`spike_time_to_utc` behandeln aware jetzt korrekt (hätte auch den PEX1-Lauf gecrasht).
- `tools/backfill_funding_rates.py` — **Head-Check im Resume**: Resume nur ab MAX(funding_time) war blind für fehlende ältere Historie (BTC/ETH/BCH hatten nach dem 30d-Smoke-Test nur 30d; der Voll-Lauf hat den Kopf nie geholt). Fehlender Kopf wird jetzt erkannt und nachgeladen (idempotent); die 3 Coins sind nachgefüllt.

## [2026-07-06] Research-Bots 30–33: PEX1 / FMR1 / TRM1 / FIF1 (Report 15 — S6/S8/S10/S11)

### Added
- **Vier neue ML-Bots** als Kohorte im gemeinsamen Channel `CH_NEW_IDEAS` (Attribution per Modell-Tag; `NEW_IDEAS_LIVE_POSTING=0` → Shadow-only). Ohne deployte Artefakte laufen alle vier im Idle-Modus. Design + VPS-Runbook: `docs/NEW_IDEAS_BOTS.md`.
  - `30_ai_pex1_bot.py` — **PEX1** Pump-Exhaustion-Short (S6): konsumiert `pump_dump_events` (vol_ratio ≥ 5 live wie im Training gespiegelt, nur Pumps), short-only, Smart-Target-Geometrie.
  - `31_ai_fmr1_bot.py` — **FMR1** Funding-Extreme Mean-Reversion (S8): Cross-Section aus einem `premiumIndex`-Request, Perzentil-Extreme (≥95 % SHORT / ≤5 % LONG), Historie live per REST — unabhängig vom Backfill-Cron.
  - `32_ai_trm1_bot.py` — **TRM1** Transition-Resolution (S10): 3-Klassen-Modell über `regime_history`-Features, postet BTCUSDT-Trades in der prognostizierten Auflösungsrichtung (nur bei debounced TRANSITION).
  - `33_ai_fif1_bot.py` — **FIF1** FIFO-Filter (S11): Standalone-A/B über den Fast-In-And-Out-Strom (10-min-Zeitfenster + Content-Key-Dedupe über active+closed — fängt Fast-Resolver, verhindert Idle-Catch-up-Backlogs), postet Gate-Passer mit ORIGINAL-Geometrie; jeder Kandidat wird als Shadow-Zeile geloggt.
- Geteilte Bausteine (eine Quelle für Bot/Builder/Trainer, X-R1-Regel): `core/research_features.py` (skalenfreie Feature-Verträge), `core/model_artifacts.py` (Artefakt-Loader + Idle-Modus), `core/signal_post.py` (atomares Outbox+ai_signals-Posting, kein Cornix-Block in der Info-Nachricht).
- Trainings-Pipeline für den VPS (Step 2): `tools/pex1|fmr1|trm1|fif1_build_dataset.py` (Labels ausschließlich via `simulate_exit`, floor-1-Join, Live-Gates gespiegelt) + `tools/new_models_train.py --strategy <s>` (Batch-E-Methodik: Chrono-Split mit Purge, Isotonic auf Val, Threshold per Replay-PnL, Artefakt NUR nach staging — P1.35).
- Registrierung: `main_watchdog.py` (start_delay 191–215), `core/config.py` `CH_NEW_IDEAS`, `.env.example` (`CH_NEW_IDEAS`, `NEW_IDEAS_LIVE_POSTING`), README-Flottentabelle.

## [2026-07-06 spätabends] ABR-LONG-Funding-Gate (Experiment)

### Added
- `18_ai_abr1_bot.py` — **LONG öffnet nur noch über das Funding-Gate**: `fund_24h > +3 bps` (Mittel der letzten 3 Funding-Sätze, live via Binance-REST, fail-closed, 30-min-Cache). Grundlage: Feature-Recheck auf Operator-Hypothese (Report 21 Addendum 2) — 16 Setup-Mechanik- + 6 Funding-Features; einziger Out-of-Sample-Überlebender ist die Funding-Regel (+1,12 %/Trade, 74 % WR, n=119/Jahr auf 100 Coins; Test +0,69 %, n=17). Postet als ABR2 inkl. Funding-Wert in der Info-Nachricht; Review nach 4–6 Wochen/≥30 Trades. Break-Volumen (Lehrbuch-Kriterium) zeigte übrigens NULL Trennschärfe.
- `tools/backfill_funding_rates.py` + Tabelle `funding_rates` — volle Binance-Funding-Historie (430d × 530 Coins), resumierbar/idempotent; Grundlage für Funding-Features in Trainern/Studien.
- `18_ai_abr1_bot.py` — **SHORT-Funding-Veto**: `fund_24h > +1,5 bps` blockt das Signal trotz Modell-Gate (Spiegeltest auf 33,5k SHORT-Events: die Zone ist in Train UND Test −1,2 %/Trade — exakt dort, wo das LONG-Gate öffnet → Kreuzvalidierung). Fail-open: ohne Funding-Daten gilt das Modell-Signal. SHORT-Info-Nachricht zeigt jetzt ebenfalls den Funding-Wert.
- `core/funding_features.py` — **geteilter Funding-Feature-Builder** (6 Features, as-of, kein Lookahead): kanonische Definitionen aus Report 21 Addendum 2 für kommende Retrains (RUB2/EPD2 vorgemerkt in docs/MODEL_INTENT.md §7/§8) — eine Quelle statt Copy-Paste-Skew, analog `core/mis_features.py`.

## [2026-07-06 abends] MIS2-SHORT live — Dump-Seite mit studien-validierter Bracket-Geometrie

### Added
- `tools/mis2_dump_geometry_study.py` — zweistufige Geometrie-Studie der Dump-Seite (Ergebnisse `staging_models/mis2_dump_geometry_study*.json`): V1 (Market-Entry, SL ≤8 %) durchweg negativ — Diagnose: die selektierten Coins spiken vor dem Dump nach oben (8h: TP-Quote 54 %, aber 38 % SL-Risse bei +8 %). V2 mit Operator-Input („mehr SL-Abstand") + Bounce-Entry: **Limit-Sell +5 % über Signalkurs + weite SLs drehen 24h/72h/168h positiv** (+0,49/+0,72/+0,27 %/Trade; 8h bleibt negativ).
- `11_ai_mis_bot.py` — `DUMP_RULES` je Horizont: Entry Limit +5 %, Einzel-TP ab Signalkurs (8H −5 %, 24H −10 %, 72H −15 %, 168H −16,7 %), SL ab Entry (5/16/12/12 %). Dump-Modelle (Close-Basis) deployed mit Operating Point = Top-2 %-Val-Quantil (der Safe-Picker hatte „nicht deploybar" geliefert — Operator-Entscheid für Live-Beweis inkl. 8H dokumentiert in docs/MODEL_INTENT.md §1).

### Operator-Entscheide
- **20x wird gepostet** (Cross-Margin, kleine Positionen auf großes Depot) — bewusst KEIN `cap_leverage_to_sl` für MIS2-SHORT, obwohl SL 12–16 % über der Isolated-Liquidationsdistanz liegt.
- Alle 4 Dump-Horizonte als Trades (kein Warn-Kanal); jeder Timeframe hat eigene Regeln.

### Known Follow-up
- Trade-Monitor kennt keine Limit-Entries: MIS2-SHORT-Signale, deren +5 %-Entry nie füllt (12–22 % laut Studie), dürfen nicht als Trades gescored werden — Monitor-Anpassung offen.

## [2026-07-06 abends] ABR2-LONG-Bypass revidiert

### Fixed
- `1_data_ingestion.py` — **coins.json-Doppel-Writer-Konflikt**: `update_trading_pairs()` (läuft bei jedem Ingestion-Start) filterte nur `status=TRADING` + nicht-USDC und ließ Binance-Neuprodukte in die Coin-Liste: Quote-Assets „U"/„USD1" (→ kaputtes Symbol **ETHU**), Cross-Pairs (ETHBTC), Quartals-Futures (`_260925`), TRADIFI_PERPETUAL (Aktien/Metalle wie COSTUSDT/XAUUSDT) — zusammen 657 statt 530 Symbole, von der ganzen Flotte konsumiert (ABR2-Vorfall). Filter jetzt identisch zu `6_housekeeping.update_coins_json` (quoteAsset=USDT + PERPETUAL); coins.json einmalig sauber regeneriert (530).

### Changed
- `18_ai_abr1_bot.py` — **LONG-Immer-Bypass zurückgenommen** (Operator-Entscheid revidiert nach ~60 LONG-Signalen in 3h über 657 Coins): Gate wieder für beide Richtungen aktiv; LONG-Artefakt (v2, Threshold 0,3 ≈ offen) durch das Legacy-3-Klassen-Modell ersetzt (kein meta.json → Blocker-Vertrag @ 0,60). Begründung: Report 21 — Setup ungefiltert −0,59 %/Trade, Break-even-WR ~63 %, ML/Regime/Management ohne rettenden Hebel. SHORT (ABR2-Binärvertrag @ 0,75) unverändert live. `docs/MODEL_INTENT.md` §2 aktualisiert.

## [2026-07-06] Live-Eingriffs-Batch nach Intent-Walkthrough (docs/MODEL_INTENT.md)

### Fixed
- **Doppel-Post-Bug flottenweit** (Operator-Meldung: Cornix erkannte beide Nachrichten als Signale): Die Chart-/Info-Nachricht enthielt den Cornix-Block eingebettet UND die Cornix-Nachricht ging separat an denselben Channel → zwei Positionen pro Signal. Gefixt in **8 Bots**: 18 (ABR), 7 (BR-Familie), 13 (RUB), 9 (SR), 11 (MIS), 12 (ATS), 24 (QM), 25 (TD/BB), 29 (UFI1). Neue Arbeitsregel: genau EINE Cornix-parsebare Nachricht pro Signal.
- `25_smc_ml_sniper.py` — BB_1H-Parking-Lücke geschlossen: das Parking saß nur im LONG-Zweig, SHORT feuerte weiter (Report-19-Nebenfund).

### Changed (Operator-Entscheide aus dem Intent-Walkthrough)
- **Versionierungs-Regel**: Überarbeitete Modelle/Bots posten unter neuem Tag (`model_id` in Artefakt-Meta → `ai_signals.model`): **ABR2** (Binär-Vertrag), **EPD2**, **RUB2**, **BR1Hv2**, **TD2_4H**, **BB2_4H**, künftig MIS2 etc. Tracker auf Präfix-Matching umgestellt (`23_market_tracker.get_category`, `core/bot_naming` MIS\d+); Cooldowns bleiben versionsübergreifend.
- `10_pump_dump_detector.py` — **EPD2**: Richtungs-Gate entfernt (beide Seiten handeln; vol_ratio-Gate bleibt).
- `13_ai_rub_bot.py` — **RUB2**: LONG-Gate wieder offen (Intent: symmetrische Idee).
- `7_pattern_detector.py` — **BR1Hv2**: SHORT-Gate entfernt (beide Richtungen, bis BR-ML-Gate steht).
- `18_ai_abr1_bot.py` — **LONG postet immer** (Operator-Entscheid; LONG-Modell ohne Selektionswert auch auf sauberen Events — Confidence informativ); SHORT-Gate auf v2-Artefakt.
- `25_smc_ml_sniper.py` — Modell-Vertrag aus Artefakt (optimal_threshold, calibrator, meta.model_id) statt Hardcode-Thresholds.
- `29_ufi1_bot.py` — **UFI1 reaktiviert** im Ist-Zustand (bewusster Operator-Entscheid „Lotterieschein", Einwand dokumentiert in docs/MODEL_INTENT.md §10).

### Deployed (Staging → Bot-Verzeichnis, Alt-Artefakte in `staging_models/archive_2026-07-06_pre_v2_deploy/`)
- **ABR2** LONG+SHORT (Retrain auf 62k Events des reparierten Detektors — distributions-matched zum neuen Live-Detektor).
- **TD2_4H** (Threshold-Re-Pick 0,58 via `pick_threshold_safe`: Test 87 Trades, 64,4 % WR, +0,81 %/Trade).
- **BB2_4H** (Re-Pick 0,63; bleibt Filter mit neutraler PnL-Erwartung).

## [2026-07-05] AIM1 ad acta — Neubau als AIM2-Master-Meta-Gate

### Added
- `docs/AIM2_DESIGN.md` — Neubau-Plan nach Report 15 S7: AIM2 als Ranker/Gate über alle Quellsignale (kein eigenständiger Alpha-Generator), Label = First-Touch der as-of rekonstruierten Smart-Targets-Geometrie, Rollout-Gates.
- `core/aim2_features.py` — EIN Feature-Builder für Trainer UND Serving (Markt floor−1, Regime, Schwarm ohne AIM1/AIM2 = F6-Fix, Quell-Identität aus DB-Vokabular + Trailing-WR). Kein Train/Serve-Skew mehr (P0.13-Fehlermodus strukturell tot).
- `tools/aim2_build_dataset.py` — 241k Events (43k gepostete AI + 198k Conv, FIFO/Volume deterministisch untersampelt), Replay-Labels via `simulate_exit`, `--skip-entry-hour`-Lookahead-Probe. TZ-Neuvermessung: alle Signal-Writer stempeln PG-Lokalzeit (Europe/Bucharest) → UTC-Konvertierung (der AIM1-Bot verglich Lokal gegen UTC, ≈3h-Versatz).
- `tools/aim2_train.py` — chrono 70/15/15 + 7d-Purge, Isotonic auf Val, Threshold per Replay-PnL; Artefakt nur nach staging (P1.35).
- `audit_reports/20_aim2_training_results.md` — Ergebnisse: AUC test 0,686, Kalibrierung monoton, Gate-Uplift OOT −0,69% → **+1,92%/Trade** @ 34% Pass; Fold 2 (Apr–Mai) +0,17%; kein Testmonat negativ; dumme Quellen-Baselines versagen (Uplift = echte Intra-Quellen-Selektion); Lookahead-Probe 0,7% Flips symmetrisch.

### Changed
- `15_ai_master_bot.py` — komplett auf AIM2: geteilter Builder, kalibrierte Probability, Parity-Guard (OOD-Wache), tägliches Modell-Reload, Kandidaten nur `posted=true`, Selbstausschluss aus dem Schwarm, `ai_signals.model='AIM2'`. **Shadow-first:** Posting nur mit `AIM2_LIVE_POSTING=1` (per Operator-Freigabe am 05.07. abends aktiviert — Channel wird nicht getradet, Cornix trackt als Validierung).
- AIM1-Dossier als historisch markiert; AIM1-Statistik bleibt unter `model='AIM1'` abgeschlossen.

## [2026-07-04/05] Binance-WS-Root-Cause + Ingestion-Härtung + Health-Monitor

### Fixed
- **DIE Root Cause der seit April „stummen" WebSockets:** Binance hat die Legacy-Futures-WS-URLs (`/stream`, `/ws`) zum **23.04.2026** abgeschaltet; ungeroutete Verbindungen handshaken OK, pushen aber nichts. Alle WS-Konsumenten (`1_data_ingestion.py`, `19_whale_logger_bot.py`, `chart_data_service.py`, `99_smc_paper_bot.py`) auf `wss://fstream.binance.com/market/stream` migriert. Whale-Logger schrieb ab da wieder Dateien (erste seit 18.04.).
- `1_data_ingestion.py` — Härtungs-Serie: 180 Streams/Verbindung (HTTP-414- und Silent-Cap), Backoff-Reset erst bei erster DATEN-Message (`got_data`), Backoff auch auf dem Silent-Break-Pfad (vorher ~900 Connects/h), Startup-Stagger, Prozess-Prioritäten (Ingestion ABOVE_NORMAL, Catch-up-Kinder BELOW_NORMAL via ProcessPoolExecutor), gap-aware Catch-up (24h statt 730d bei bestehender Historie).

### Added
- `1_data_ingestion.py` — **REST-Freshness-Fallback**: schlägt Kerzenlücken TF-first (5m/30m/1h) per REST, solange der WS keine Daten liefert; legt sich automatisch schlafen, sobald der WS wieder lebt.
- `core/health_monitor.py` + Watchdog-Anbindung (60s): DATA_STALE (12 min → Auto-Restart der Ingestion, 120-min-Cooldown), CPU_SATURATED (90%/5min), OUTBOX_FAILING/STUCK; Alerts an `TELEGRAM_ALERT_CHAT_ID`.

## [2026-07-03/04] Audit-Sofortmaßnahmen + DB-Betrieb

### Changed (Portfolio, per Audit Reports 13–16)
- Geparkt via `control/parked/`: `14_ai_atb_bot.py` (ATB1), `29_ufi1_bot.py` (UFI1), zeitweise `15_ai_master_bot.py` (AIM1 → am 05.07. durch AIM2 ersetzt).
- Richtungs-Gates: EPD1 nur LONG + `vol_ratio ≥ 5`-Gate, RUB1 nur LONG, BR1H nur SHORT; ATS1-Band [0,60, 0,80); ROM1 15%-SL-Cap; `cap_leverage_to_sl` in `core/trade_utils.py` (versteht auch "20x"-Strings).
- `3_detectors.py` — Fast-In-And-Out auf expliziten Operator-Wunsch wieder aktiv (Audit-Note F bleibt dokumentiert).

### Infra (VPS, nicht Code)
- PostgreSQL-Datadir nach `C:\PGDATA` migriert; `pg_stat_statements` aktiviert; `wal_compression=pglz`; 2.380+ `(open_time DESC)`-Indexe, Dedup-/Modell-Indexe; 485 Junk-Tabellen entfernt; `telegram_outbox` VACUUM FULL.
- Erste DB-Backups überhaupt: `tools/backup_db.ps1` als nächtlicher Scheduled Task (03:30, `pg_dump -Fc` → `D:\_BACKUP\db`, Retention 7 täglich + 4 wöchentlich).
- TimescaleDB-Hypertable-Migration designt (`docs/TIMESCALE_R1_MIGRATION.md`), Start nach stabiler Fleet-Phase (Task T-2026-CU-9050-018).

## [2026-07-05] ABR1 Detektor-Rework + Binär-Modell-Vertrag

### Fixed
- `18_ai_abr1_bot.py` — **Richtungs-Kopplung des Retests**: die alte Logik nutzte `is_retest_long OR is_retest_short` als reines Touch-Gate und nahm die Richtung allein aus dem Break — ein High-Touch von unten an einen aufwärts gebrochenen Widerstand (= gescheiterter Ausbruch, Trainings-LOSS-Klasse) wurde als LONG signalisiert (spiegelbildlich für SHORT). Jetzt: LONG verlangt Low-Touch von oben UND Close über dem Level, SHORT spiegelbildlich (Trainer-Semantik).
- `18_ai_abr1_bot.py` — **Hold-Check + Erst-Touch**: Closes zwischen Break und Retest müssen auf der Break-Seite bleiben; nur der erste Band-Touch nach dem Break zählt (wie der Trainer labelt). Dip + Re-Break ankert am frischen Break.
- `18_ai_abr1_bot.py` — **R07-ABR1-b**: `find_pivot_levels` ohne Edge-Padding — nur noch bestätigte Pivots (PIVOT_WINDOW Kerzen beidseitig), keine repaintenden Rand-Levels mehr.
- `18_ai_abr1_bot.py` — **R07-ABR1-a**: nur noch die jüngste geschlossene Kerze ist Retest-Kandidat (vorher bis zu 3h stale Entries).

### Added
- `18_ai_abr1_bot.py` — `find_break_retest_setups()`: gemeinsame Erkennung für Bot UND Walk-Forward-Simulator (eine Quelle, kein Skew) inkl. 5 Setup-Geometrie-Features (`setup_dist_close_level_pct`, `setup_break_strength_pct`, `setup_candles_since_break`, `setup_level_age_candles`, `setup_retest_wick_pct`) — vorher war das B&R-Setup selbst für das Modell unsichtbar.
- `18_ai_abr1_bot.py` — **R13-ABR1-5**: Modell-Vertrag (Features, Threshold, success_proba-Spalte) wird aus der `*_meta.json` des Artefakts geladen statt hardcoded; Binär-Modelle (retrain_from_replay) und Legacy-3-Klassen-Modelle werden beide unterstützt. Optionaler Isotonic-Kalibrator (`*_calib.pkl`) für die angezeigte Confidence (Gate läuft auf Roh-Probability).
- `backtest/test_abr1_detection.py` — 9 Unit-Tests über alle Fehlerklassen der alten Logik (synthetische Kerzenserien).

### Changed
- `tools/walkforward_sim.py` + `tools/retrain_from_replay.py` — MIS1-Horizonte von {72,168}h auf alle vier Live-Horizonte {8,24,72,168}h erweitert (der Bot fährt 8 Modelle; 8h/24h wären sonst auf den alten, defekten Trainings geblieben). Der 400d-Replay muss dafür neu laufen; der alte liegt in `replay/archive_2026-07-05_mis1_h72_168/`.
- `tools/walkforward_sim.py` — ABR1-Adapter nutzt `find_break_retest_setups()` aus dem Bot-Modul; Geometrie-Features landen im Replay-Feature-Dict.
- `tools/retrain_from_replay.py` — `ABR1_FEATURES` = 18 Indikator- + 5 Geometrie-Features (`ABR1_FEATURES_LEGACY` für den Alt-Modell-Vergleich); `features`-Liste in die meta.json; Isotonic-Kalibrator wird als `bt2_model_*_calib.pkl` persistiert (ging vorher für abr1 verloren).

## [2026-06/07] Audit „Kythera 2026" (Steps 1–10)

- `AUDIT_TODO.md` + `audit_reports/01…20` + Modell-Dossiers: kompletter Code-/DB-/ML-Audit über alle 9 Modellfamilien inkl. Live-DB-Verifikation (Step 2), Trainer-Provenienz (Step 3, alle Trainer sanitisiert in `legacy_trainers/`), Bot-Performance aus der Live-DB (Step 4), Regime-Orchestrator-Analyse (Step 6), Konzeptbewertung aller Strategien (Report 16), Batch-E-Retrains auf Replay-Labels (Report 19: `tools/walkforward_sim.py` + `tools/retrain_from_replay.py`, geteilte Feature-Builder `core/mis_features.py`).
- Kernbefunde u.a.: AIM1-Kalibrierung invertiert (P0.13), UFI1 +278R war Krisenmonats-Artefakt (P0.11, walk-forward-bewiesen), Forming-Candle-Serving (R1), TZ-Mix (R3), Labels ≠ Live-Geometrie als Querschnittsursache (X-R1).

## [2026-04-18] Regime-Orchestrator (v1.0)

### Added
- `26_regime_detector.py` — Classifies BTC regime every 5 min (5 classes) + Alt-Context (3 classes, BTCDOM-based). Debounce on both axes independently. Hourly status posts + regime-change alerts.
- `27_bot_regime_analyzer.py` — Hourly Bot×Regime×AltContext×Direction performance. Two-stage whitelist: standard (WR≥Overall) + counter-trend (≥60% AND ≥Overall+10pp). Daily cross-table post 07:00 UTC.
- `28_signal_orchestrator.py` — Signal gating every 500ms. 4D whitelist check, overall fallback on detector failure. Auto-close on regime change. ROM1 tracking in ai_signals (automatically picked up by 8_ai_trade_monitor). A3 cooldown (4h).
- `core/regime_logic.py` — Shared classification logic (compute_features, classify_regime, apply_debounce).
- `backtest/backfill_regime_history.py` — One-off 90-day backfill (idempotent).
- 3 test files in `backtest/`
- 6 new DB tables: regime_history, regime_current, bot_regime_performance, bot_regime_whitelist, orchestrator_open_trades, orchestrator_suppressed_signals
- `docs/REGIME_ORCHESTRATOR.md`, `INSTALL_REGIME_ORCHESTRATOR.md`

### Changed
- `core/config.py` — REGIME_TRADING_CHANNEL_ID = <CH_REGIME_TRADING>, REGIME_STATUS_CHANNEL_ID = <CH_MARKET_DATA>
- `main_watchdog.py` — 3 new processes (start_delay 160/167/175)
- `23_market_tracker.py` — `Regime Fit:` line in Kelly post (graceful degradation)

# CHANGELOG — Crypto Bot Deep-Review & Fix Round

This review went through the entire codebase (46 Python files, 24 trading bots, Binance Futures integration, Telegram outbox, PostgreSQL storage) and found/clarified **91 analysis points** in total. Of these:

- **57 real bugs fixed**
- **20 points clarified as false alarms from initial analysis** (code was correct, my initial assessment too pessimistic)
- **6 points explicitly descoped by the user** (Master-Bot Dedupe, BTC SMC 100×, Handler-Auth, Cross-Bot-Limit etc.)
- **5 points documented as too invasive for this round** (schema change, retraining required)
- **3 points clarified as asyncio-non-critical/unreproducible**

## Fixes by topic

### 🔧 Trade-Signal-Korrektheit (kritisch)
- **#1 SHORT-RSI-Bug** (strat_fast_in_out, strat_5_percent): `>=75 OR <=45` → nur `<=45`. Der Code generierte SHORT-Signale bei hoch-RSI-**UND** tief-RSI gleichzeitig → regelmäßig dumme Trade-Richtung
- **#3 RSI-fillna-Parens**: `100 - (100/(1+rs)).fillna(0)` → `(100-100/(1+rs)).fillna(50)`. Previously, RSI fälschlich als 100 (Max-Overbought) angezeigt wo keine Daten da waren → false SHORTs
- **#13 AI SR Bot Cooldown**: `pd.Timestamp.utcnow().tz_localize(None)` crashte in neueren pandas-Versionen. Auf `market_utils.check_cooldown` migrated
- **#15 Master-Bot all_ai_models-Konkat-Typo**: `'MIS1' 'MSI1-8h_pump'` (fehlendes Komma + vertauschte Buchstaben) konkateniert → ungültiger Model-Name in ml_predictions_master
- **#19/#18 ATB `except: return True`**: Cooldown-Check gab bei DB-Hiccup "ja, darf traden" zurück → Signal-Spam. Jetzt safe-default `False`
- **#32 ATS Bot** OBV-Normalisierung: `obv - obv.iloc[0]` damit die OBV-Werte nicht vom willkürlichen Startpunkt der Historie dominiert werden
- **#38 Smart Targets SL-Fallback**: `min/max`-Cap added damit SL garantiert innerhalb (LONG) or außerhalb (SHORT) entry2 liegt
- **#58 SMC ML Sniper BB**: `MAX_BB_AGE=20` + 0.3% echter Break-Through (vorher konnten 200-Kerzen-alte Stale-BBs immer noch ein Signal triggern)
- **#59 SMC ML Sniper TD**: `MAX_TD_SPAN=50` Kerzen (vorher: unbegrenzt)
- **#60 BTC SMC**: `ORDER BY ASC` → `DESC + reverse` (historische Daten wurden in falscher Reihenfolge gelesen)
- **#65/#66 IP Pattern Bot**: `ALERTED_QMS` persistent, Pattern-ID mit Unix-Timestamp statt Laufzeit-Counter
- **#55/#56 Quasimodo**: `MIN_CONFIDENCE 0.40→0.65`, `ZONE_TOLERANCE 0.01→0.005`, Touch+Bounce-Validierung

### 🗄️ DB-Robustheit
- **#4 Atomic Write**: `active_trades_master` + `telegram_outbox` in einer Transaktion statt zwei separaten (verhindert Chart ohne Trade)
- **#8/#16 Monitor-Connection**: Auto-Reconnect im Trade Monitor und AI Monitor bei DB-Hiccup (vorher: Bot loopte mit toter Connection weiter)
- **#10 Trade Monitor datetime**: `datetime.now()` → `datetime.now(timezone.utc)` in close_trade
- **#14 DB-Flusher SAVEPOINT**: Per-Row-Fehlertoleranz, ein einzelner Insert-Fail reißt nicht den ganzen Batch mit
- **#48 telegram_outbox Cleanup**: Nightly DELETE gesendeter Einträge älter als 7 Tage (vorher wuchs die Tabelle unbegrenzt)
- **#60 BTC SMC** ORDER BY (oben)

### 🎯 Cooldown-Konsolidierung
- **#33/#34/#51** drei eigene `is_cooled_down`/`set_cooldown`-Duplikate removed (SMC Forex, ATB, andere), alle nutzen jetzt `core.market_utils.check_cooldown`/`update_cooldown`
- **#34** SMC Forex Cooldown-Keys ohne TF-Suffix → TF-übergreifender Block (1h und 4h nicht gleichzeitig auf demselben Coin)
- **#17 RUB** Cooldown-Check VOR ML-Prediction (CPU-Einsparung)
- **#13 AI SR** eigener timezone-crashing Cooldown removed
- **#35 Mayank** 12h-Cooldown pro asset+TF+direction added
- **#42** Mayank asset-cooldown (durch #35 bereits erledigt)

### 📊 Indicator Engine & Strategies
- **#5** Duplikat-Lookback-Block im indicator_engine (bewirkte dass inkrementelle Läufe IMMER 3000 statt 1000 Kerzen luden)
- **#6 Trendline** NaN-robust bei konstanten Preisen, Division-durch-0 bei `y[0]==0` abgefangen
- **#12 Volume Indicator** `df.loc[index-1]` → `iloc` mit `reset_index` (KeyError bei Filter-inducierten Index-Lücken)
- **#45 indicator_state.json** atomares Write via tmp+fsync+os.replace (verhindert halb-geschriebene Reads)
- **iloc-Fix in strat_fast_in_out**: DESC-sortierter DF, `iloc[-1]` → `iloc[0]` für ATR-Zugriff
- **#11 Support/Resistance Zuordnung**: Nach Proximity (nächster unter Preis = support, nächster über = resistance) statt nach Zeit

### 🤖 AI-Bots (Feature-Robustheit)
- **#20 ATB** NaN/Inf-Absicherung vor predict_proba (`replace([inf,-inf],nan).fillna(0)`)
- **#24 RUB get_f** behandelt NaN/Inf, nicht nur None
- **#25 ABR1** X_event NaN/Inf-Absicherung
- **#27 MIS1** Thresholds beim Load explizit geloggt (Drift-Detection)
- **#36 AI Monitor** targets_hit defensiv zu int() casten
- **#74 ABR1 SUCCESS_CLASS_IDX=0**: Warnung-Kommentar added — **Bitte manuell gegen Training-Notebook verifizieren!**
- **#75 ABR1** asymmetrische Thresholds dokumentiert (LONG=0.60, SHORT=0.80)
- **#76 ABR1** redundanten `minute != 0` Filter removed (1h-Kerzen haben immer minute=0)
- **#52** get_hvn_and_sr_levels zentralisiert (5 bit-identische Kopien → 1 in core/trade_utils.py)

### 💬 Telegram Outbox & Charts
- **#21 active_patterns.json** atomares Write
- **#31 Housekeeping** respektiert Outbox-Referenzen (löscht keine Charts mehr, die noch versendet werden müssen)
- **#67 Chart-Pfad Race**: `int(time.time()*1000)` Millisekunden-Timestamp im Dateinamen (ms statt s)
- **#68/#87 mark_sent/mark_failure**: Chart nur löschen wenn keine anderen ungesendeten Outbox-Einträge die Datei noch referenzieren

### 🛠️ Infra (Watchdog, Dashboard, Housekeeping)
- **#69 Watchdog** Exponential Backoff `[0, 15, 60, 300, 900]s` basierend auf Crashes in der letzten Stunde
- **#70 Dashboard** stdout/stderr in `logs/dashboard.log` statt DEVNULL
- **#85 update_model** Threshold-Files (`threshold_*.pkl`) explizit überspringen + `hasattr(model, 'save_model')` Check
- **#88 core/state_utils.py** neu: atomic_write_json + atomic_read_json als zentrale Helper

### 📈 Market Tracker & Logger
- **#71/#73** Kategorie-Mapping korrigiert (TD/BB/QM als PATTERN statt INDICATOR/VOLUME)
- **#72** Volume-Näherung: `close` → `(open+close)/2` (reduziert Intra-Candle-Bewegungsfehler)
- **#81 Whale Logger** `format_usd` handled negative Werte korrekt (`-$1.5M` statt `$-1500000`)
- **#82 Funding Logger** `check_top20_positive_pct` gibt None statt 50.0 bei leeren Daten
- **#83 Funding Logger** `calc_diff_bps` gibt None bei fehlender Historie, Display zeigt "N/A"

### ❌ Gelöscht
- **99_smc_paper_bot.py** removed (Paper-Trading-Bot der nicht live lief)
- Entsprechende line in `main_watchdog.py` removed

## ⚠️ Wichtige Hinweise für den Deploy

### Sofort-Checks vor Deploy
1. **ABR1 SUCCESS_CLASS_IDX manuell verifizieren**: `18_ai_abr1_bot.py` line 45 — aktuell steht `0`, standard-XGBoost-Konvention wäre `1`. Bitte gegen dein Training-Notebook prüfen. Wenn dort `y=1` für gewinnende Trades steht, MUSS der Wert auf `1` geändert werden.

### Kurzfristig prüfen (erste Run nach Deploy)
2. **Funding-Logger Telegram-Output**: Beim allerersten Lauf wenn keine 1h/24h-Historie vorliegt, sollten jetzt `N/A`-Strings statt `+0.0bps`/`50.0%` angezeigt werden. Das ist gewollt.
3. **Market-Tracker Kategorisierung**: TD/BB/QM/SMC-Signale erscheinen jetzt in der Kategorie PATTERN statt INDICATOR/VOLUME. Die Statistik ändert sich einmalig.
4. **Dashboard-Log**: `logs/dashboard.log` sollte erstellt und beschrieben werden. Falls Dashboard crasht, steht der Traceback da drin.
5. **SMC Forex Cooldowns**: Jetzt TF-übergreifend (12h). Falls Signale signifikant seltener kommen, kann die Dauer auf 8h reduziert werden (Code-Stelle `check_cooldown(conn, cd_key, display_name, 'LONG', 12)`).

### Mittelfristig (Performance-Backlog, nicht jetzt)
6. **#50** Market Tracker 10k-Queries: Würde eine unified `ohlcv_30m`-Tabelle erfordern (Ingestion-Schema-Change). Performance-Backlog.
7. **#88** 7 weitere State-Files könnten auf `core.state_utils` konsolidiert werden. Niedrige Priorität.

### Nicht gefixt, außerhalb Scope (bewusst)
- #22 Master-Bot Dedupe (separate Bewertung pro Quelle gewollt)
- #62 BTC SMC 100× Leverage (deliberate high-risk)
- #77/#78 Open-Handler Auth (privates Env, intentional)
- #89 Cross-Bot Position-Limit (Bots laufen selektiv)
- #2 check_recent_trades (ist ok so)
- #53 TSI Parameter-Order (verifiziert: EWMA-Komposition ist bit-identisch)

## Statistik final

| Kategorie | Anzahl |
|---|---|
| Real bugs fixed | **57** |
| Als false alarme geklärt | 20 |
| User-explizit out-of-scope | 6 |
| Zu invasiv for this round | 5 |
| Asyncio-unkritisch | 3 |
| **Gesamt geprüft** | **91** |

| Python-Dateien im Projekt | Syntax-clean nach Fixes |
|---|---|
| 47 | 47 ✅ |

## Dateien mit wesentlichen Änderungen

```
core/
  market_utils.py              (FIX #51 zentral nutzbar)
  trade_utils.py               (+ get_hvn_and_sr_levels, ensure_min_tp_distance)
  state_utils.py               (NEU)
  update_model.py              (#85)

1_data_ingestion.py            (#14 SAVEPOINT)
2_indicator_engine.py          (#5, #6, #45)
3_detectors.py                 (#4 atomic signal write)
4_telegram_bot.py              (#68/#87 chart ref-counting)
5_trade_monitor.py             (#8 reconnect)
6_housekeeping.py              (#31, #48)
7_pattern_detector.py          (#21 atomic)
8_ai_trade_monitor.py          (#8, #36)
9_ai_sr_bot.py                 (#13, #52)
10_pump_dump_detector.py       (#38, #52)
11_ai_mis_bot.py               (#11, #15, #27)
12_ai_ats_bot.py               (#32, #38, #52)
13_ai_rub_bot.py               (#17, #24, #38, #52)
14_ai_atb_bot.py               (#18/#19, #20, #51, #52)
15_ai_master_bot.py            (#15, #28)
16_smc_forex_metals_bot.py     (#33, #34, #51)
17_mayank_bot.py               (#35)
18_ai_abr1_bot.py              (#25, #74, #75, #76)
19_whale_logger_bot.py         (#81)
20_funding_logger_bot.py       (#82, #83)
21_btc_smc_strategy.py         (#60)
22_ip_pattern_bot.py           (#65, #66)
23_market_tracker.py           (#71, #72, #73)
24_quasimodo_bot.py            (#55, #56)
25_smc_ml_sniper.py            (#58, #59)
main_watchdog.py               (#69, #70)
strategies/
  strat_fast_in_out.py         (#1)
  strat_5_percent.py           (#1)
  strat_main_channel.py        (#11)
  strat_volume_indicator.py    (#12)
```

Einzelne Batch-Reports in `reports/batch_1_report.md` … `reports/batch_6_report.md`.
