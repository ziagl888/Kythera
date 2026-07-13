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
