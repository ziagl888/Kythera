# 18 — DB-Architektur, Performance & Berechnungs-Konsistenz (Step 9)

**Stand:** 2026-07-04 · **Methode:** (A) Live-Messungen direkt auf der VPS-DB (PostgreSQL 17.6, `cryptodata`, Statistik-Fenster seit Reset 2026-06-21; Fleet war zum Messzeitpunkt gestoppt), (B) TimescaleDB-Ist-Analyse, (C/D) zwei parallele Code-Reviews zur Berechnungs-Konsistenz über alle Bots (Geld-Mathematik + Indikator-Varianten). Schließt die in Frage-Katalogen von Report 02/12 offen gebliebenen Performance-Punkte.

---

## A. Live-DB-Messungen (Fakten)

| Messgröße | Wert | Einordnung |
|---|---|---|
| DB-Größe | **25 GB** | davon ~22,4 GB in per-Symbol-Tabellen (13 GB `_indicators`, 6,8 GB Candles 15m–1w, 2,6 GB 5m) |
| Tabellen gesamt | **9.782** | 5.522 Candle- + 4.090 Indikator-Tabellen + ~170 Sonstige |
| Hypertables | **0** | TimescaleDB 2.26.3 installiert, **komplett ungenutzt** (Abschnitt B) |
| WAL-Volumen | **110 GB in 13 Tagen ≈ 8,5 GB/Tag** | 4,4× DB-Größe pro Monat — Churn-getrieben, `wal_compression=off` |
| Dead Tuples gesamt | 5,86 Mio (9,2% von live) | autovacuum (4 Worker) kommt bei 9.782 Tabellen strukturell schwer nach |
| Katalog-Größe | **685 MB** (pg_catalog) | Folge des Tabellen-Sprawls (~100k Katalogeinträge für Spalten/Indexe) |
| Tote Tabellen | **1.010 Tabellen, 1,75 GB** (0 Rows, 0 Inserts) | Alt-Generationen (`conv_signals`, `bot_trades4`, `PAXGUSDT_5m_GOLD`, …) |
| Quarterly-Reste | **108 Tabellen** (`BTCUSDT_260925_*` u.a.) | Folge von P2.16 (coins.json-Writer-Drift inkl. Quarterlies) |
| max_connections / RAM-Settings | 200 · shared_buffers 16 GB · effective_cache_size 48 GB · work_mem 64 MB | RAM-Seite solide dimensioniert |

### A1 — Die fünf Performance-Findings (neu, mit Beweis)

- [ ] **D1 (HIGH): `closed_trades_master` + `closed_ai_signals` werden ausschließlich per Full-Table-Scan gelesen.** Seit 21.06.: `closed_trades_master` 1,80 Mio seq_scans / **215 Mrd. gelesene Tupel** / 0 idx_scans; `closed_ai_signals` 1,52 Mio seq_scans / **219 Mrd. Tupel** / 0 idx_scans. Jede Cooldown-Prüfung der Classic-Strats (pro Coin pro Zyklus!), jede Analyzer-/Tracker-Auswertung liest die komplette Tabelle. Das sind ~33 Mrd. Tupel/Tag reine Scan-Last — der Hauptgrund, warum die Detector-Zyklen und der stündliche Analyzer teuer sind. **Fix:** Indexe `closed_trades_master(strategy, posted)`, `closed_ai_signals(model, close_time)` bzw. `(symbol, model, direction, open_time)` — Letzterer gleich als **UNIQUE** (erledigt zugleich den Duplikat-Backstop aus Report 14 A.1). `active_trades_master` (321k seq_scans) profitiert von `(symbol, strategy)`.
- [ ] **D2 (HIGH): `telegram_outbox` ist auf 304 MB für 17k Rows aufgebläht** (≈18 KB/Row; 27,7k Updates + 25,8k Deletes im Fenster, nur PK-Index). Der Dispatcher-Poll (500ms-Loop von 4 und 28) läuft über eine 300-MB-Heap. **Fix:** `VACUUM FULL telegram_outbox` (bei gestoppter Fleet, Sekunden) + Partial-Index `ON telegram_outbox (id) WHERE sent=FALSE AND failed=FALSE` + `fillfactor=70`. Danach sollte die Tabelle dauerhaft <10 MB bleiben.
- [ ] **D3 (MEDIUM): Write-Amplification des 3s-Upsert-Loops gemessen:** jede 5m-Kerze wird **~15× überschrieben** (pro 5m-Tabelle 52.945 Updates auf 3.503 Inserts im Fenster, × 698 Symbole ≈ 2,8 Mio Updates/Tag nur für 5m). Zusammen mit dem 12h-Catch-up (7-Tage-Rewrite) erklärt das die 8,5 GB WAL/Tag. **Fix kurzfristig:** Upsert nur bei geändertem Close/Volume (`WHERE`-Klausel im UPDATE-Teil), `wal_compression=on` (eine Zeile, ~50% WAL-Ersparnis); **richtig gelöst** wird es durch R1 (nur geschlossene Kerzen schreiben) bzw. Abschnitt B.
- [ ] **D4 (MEDIUM): `bot_regime_performance` ist ein Update-Hotspot:** 830k Updates + 548 autovacuums auf einer Kleinst-Tabelle (der stündliche Analyzer schreibt jede Zelle einzeln neu). **Fix:** `TRUNCATE`+Bulk-`INSERT` pro Lauf oder `ON CONFLICT`-Batch; `fillfactor=50` für HOT-Updates.
- [ ] **D5 (LOW): Datenmüll räumen:** 1.010 tote Tabellen (1,75 GB) + 108 Quarterly-Tabellen droppen (Liste per `n_live_tup=0 AND n_tup_ins=0` generierbar); `pump_dump_events` (829 MB, größte Tabelle, rsi/tsi-Spalten nie befüllt — P1.40) mit Retention begrenzen; `master_ai_processed_signals` (138 MB, 920k Rows) Retention prüfen. Zusammen ~2,7 GB sofort + laufendes Wachstum gestoppt.

---

## B. TimescaleDB: installiert, ungenutzt — Bewertung & Migrationspfad

**Ist-Zustand:** Extension `timescaledb 2.26.3` ist geladen (steht in `shared_preload_libraries`), aber **0 Hypertables** — die gesamte Zeitreihen-Last läuft über 9.612 plain Tabellen. Das ist die schlechteste beider Welten: Man zahlt den Extension-Overhead und nutzt keinen einzigen Vorteil.

**Warum die aktuelle Architektur (Tabelle pro Symbol×TF) die Probleme aus A erzeugt:**
1. 9.782 Tabellen → 685 MB Katalog, ~100k Autovacuum-Zieltabellen für 4 Worker, kein globales Query („alle Coins mit Gap") ohne 698-fache Schleife — genau das Muster der 8.600 seriellen `to_regclass+MAX`-Queries im Catch-up (Report 02).
2. Schema-Änderungen (z.B. `is_closed`-Spalte aus R1) müssen 9.612× ausgerollt werden.
3. Kein Compression/Retention-Konzept: 13 GB Indikator-Tabellen sind kalte, append-only Historie — ideal komprimierbar, liegen aber unkomprimiert im Heap und WAL.

**Empfehlung (Zielbild):** Zwei Hypertables statt 9.612 Tabellen:
- `candles(symbol text, tf text, open_time timestamptz, o/h/l/c/v, is_closed bool, PRIMARY KEY(symbol, tf, open_time))` — `segmentby=symbol`, `orderby=open_time`, Chunk 7d, Compression nach 14d, Retention nach Bedarf.
- `indicators(symbol, tf, open_time, …spalten…)` analog.
- Erwartete Effekte: Compression bei OHLCV/Indikator-Daten typisch **90%+** (25 GB → realistisch 4–6 GB), WAL sinkt massiv (komprimierte Chunks werden nicht mehr re-geschrieben), autovacuum-Last kollabiert (2 Tabellen statt 9.612), globale Queries (Gap-Census, Staleness-Monitoring, Cross-Coin-Features) werden einzeilig, `is_closed` (R1) ist eine einzige Spalte.

**Aufwand & Risiko (ehrlich):** Das ist **kein Quick-Fix, sondern ein Umbau** — alle ~40 f-String-Tabellennamen-Zugriffe (`f'"{sym}_{tf}"'`, Report P3.3) müssen auf `WHERE symbol=%s AND tf=%s` umgestellt werden (Ingestion, Engine, Housekeeping, alle Bots, Trainer). Realistisch: Core-Helper `candles_read(sym, tf, n)` in `core/` bauen, Migration per Dual-Write (neue Hypertable parallel befüllen, Reader schrittweise umziehen, alte Tabellen erst nach Verifikation droppen). Sinnvoller Zeitpunkt: **zusammen mit dem R1-Fix**, weil beide denselben Code anfassen. Bis dahin liefern D1–D5 + `wal_compression=on` den Großteil der kurzfristigen Entlastung ohne Architektur-Risiko.

---

## C. Konsistenz-Matrix „Geld-Mathematik" (Code-verifiziert)

### C1 — PnL-Berechnung
**Positiv:** Die Kern-Formel (Preis-Delta/Entry×100, richtungsnegiert, ohne Leverage/Fees) ist in 5, 8, 23, 27, 28 identisch; Entry2 fließt nie in gemessenes PnL ein.

- [ ] **K1 (CRITICAL): Orchestrator-Outcome per Zufallsmatch.** `28_signal_orchestrator.py:883-918`: `sync_closed_trades` klassifiziert ROM1-Trades anhand des PnL eines **beliebigen** coin/direction-gleichen Fremd-Trades (30-Tage-Fenster, `LIMIT 1` ohne `ORDER BY`). Die Selbstbewertung des Meta-Layers ist nichtdeterministisch. (Verschärft P1.8.)
- [ ] **K2 (HIGH): SL-Trailing divergiert zwischen den Monitoren.** `5_trade_monitor.py:243-247` lässt den SL nach TP1 für immer auf Entry (Breakeven), `8_ai_trade_monitor.py:203-226` trailt auf das vorherige Target. Identische Marktverläufe → Classic ≈0% PnL, AI deutlich positiv. **Alle Bot-Vergleiche (23, 27, Whitelist) sind dadurch strukturell verzerrt** — Classic wird systematisch schlechter gemessen als AI. (Präzisiert P1.2: der 5er-Monitor trailt nicht „falsch", sondern gar nicht.)
- [ ] **K3 (HIGH): „Win" ist dreideutig.** Status `n` aus `5_trade_monitor.py:222` heißt „nach TPn per SL gestoppt" (close=Entry → PnL≈0). Derselbe Trade ist: **Win** im Classic-Cooldown (`strat_fast_in_out.py:46`, status 1–4), **neutral** in Tracker/Analyzer (pnl>0,1%-Regel, `23:941-959`/`27:126-153`), **Loss** in Legacy-Auswertungen ('SL1'→NaN). Cooldown-Steuerung und Performance-Reporting arbeiten mit widersprüchlichen Erfolgsbegriffen.
- [ ] **K4 (MEDIUM): Legacy-Pfad in 8 misst anders als der moderne** (`8:175-184`: Close-basiert, ±2,5/−5%-Schwellen; modern: wick-aware am Level) — Alt- und Neu-AI-Trades nicht vergleichbar.
- [ ] **K5 (MEDIUM): Trainer-PnL inkompatibel zur Live-Metrik:** `qm_ml_trainer.py:224-238` rechnet USD mit 20x + Fees, `smc_ml_trainer.py:294-296` in R-Multiples mit **definierten aber unbenutzten** Fee/Leverage-Konstanten, live wird ungehebeltes Preis-% gemessen — Trainer-Thresholds und Live-Winrates sind nicht dieselbe Größe.

### C2 — TP/SL-Konstruktion (Familien-Drift)

| Familie | TP | SL | Entry2 | Auffälligkeit |
|---|---|---|---|---|
| smart-targets (7, 11, 15, 18, 25, open_handler) | S/R+Fib+HVN+FVG, ATR-Spacing | ATR 3×, **Hard-Cap 15%** | ATR 1,5×, Cap 10% | Referenz-Implementierung |
| get_hvn-Familie (9, 10, 12, 13, 28) | S/R-Zonen, bis 20 TPs | nächste Zone, **kein Max-Cap!** | fix ±5% | ROM1-SL bis 65% belegt (P2.27) |
| **14 ATB** | wie get_hvn | Fallback **±5%** statt ±2,5% | **±4%** statt ±5% | undokumentierter Familien-Bruch (`14:525-529`) |
| Classic (5 Strats) | fix % oder Zonen | ATR-Caps 2,5–5% bzw. fix | nein | — |
| 16/17/21 SMC | Pivot-Level | Gegenpivot, kein Sanity-Cap | 16: „Entry 2" **nur im Telegram-Text** | 21 einziger 100x-Bot, alle drei ohne Tracking |

- [ ] **K6 (MEDIUM):** Zwei SL-Philosophien für dieselbe `ai_signals`-Tabelle: smart-targets cappt bei 15%, die get_hvn-Familie hat keinen Cap (Beleg für R4/P2.27). ATB weicht zusätzlich unbegründet von der eigenen Familie ab.

### C3 — Cooldowns
Drei inkompatible Welten: core `check_cooldown` (True=**blockiert**, DB, TZ-sicher) · 99_paper (True=**erlaubt**, RAM, Seiteneffekt beim Check) · Classic-Strats (**globaler Win-Zähler** statt Zeit-Cooldown, TZ-naiv → Fenster um Server-Offset verschoben, und „Win" inkludiert Breakeven-Stopouts per K3). Cooldown-Dauern derselben Signal-Familie: 15min (EPD1) bis 48h (UFI1), undokumentiert; der Orchestrator legt seine 4h obendrauf.

### C4 — Entry-Preis-Quellen (Staleness-Klassen)

| Frische | Bots |
|---|---|
| Sekunden (REST/Ticker) | 3_detectors (Classic), 10 EPD1 |
| ≤5 min (5m-Close) | 28 ROM1 |
| ≤1 h (1h-Close) | 11, 12, 13, 14, 18, 29 |
| **≤4 h** (Scan-TF-Close) | 24 QM, 25 TD/BB |
| ≤60 min (fremder Entry) | 9 SRA1 (übernimmt Classic-Entry) |

- [ ] **K7 (HIGH): Entry-Staleness bis 4h bei sofortiger 5m-Prüfung.** Der Monitor (`8:79-110`) prüft alle Trades sofort gegen die aktuelle 5m-Kerze — ein bis zu 4h alter „CMP-Entry" kann beim Posten schon durch den SL gelaufen sein → Phantom-SL-Hits, PnL gegen nie handelbare Preise. Die Winrates der Familien sind allein wegen der Frische-Klassen nicht vergleichbar.
- [ ] **K8 (LOW):** R:R-Gate rechnet mit `avg(entry1,entry2)` (`11:334-336`, `25:351-353`), gemessen wird gegen entry1 — das Gate bewertet einen anderen Trade als den gemessenen.

---

## D. Konsistenz-Matrix Indikator-Berechnungen (Code-verifiziert)

### D-Kernbefund: Derselbe Indikator-Name, mehrere Mathematiken

| Indikator | Varianten im Repo | Folgenschwerste Abweichung |
|---|---|---|
| **RSI** | Engine `ewm(span)` (DB, alle Leser) · pandas_ta **Wilder** (ATB 14:177, ABR1 18:115, RUB1-Training) · echtes Wilder (backtest v3) | DB-`rsi_14` verhält sich wie Wilder-RSI(7–8), Δ ø 4,84 Punkte (Step 2). Alle DB-RSI-Schwellen (RUB1-Gate <30/>70, 5-Percent-Band 55–75, MIS/QM/SMC-Features) feuern in einer anderen Population als jede Chart-/pandas_ta-kalibrierte Schwelle. |
| **ATR** | Engine **Wilder** `ewm(alpha=1/p)` (2:420) · **SMA-ATR** in `core/trade_utils.py:38-45` (→ `calculate_smart_targets` → SL/Entry2 von 7/11/15/18/25/open_handler) und `21:42-55` · **ewm(span)** in `core/regime_logic.py:105-109` (Regime-P75/P40-Gates) | **Drei ATR-Definitionen**: „3×ATR-SL" bedeutet je Subsystem etwas anderes; Kalibrierungen (Multiplikatoren, Percentile) sind zwischen Engine-Features, SL-Sizing und Regime-Klassifikation nicht übertragbar. |
| **MACD** | Engine 9/21/9 („fast") + 12/26/9 („normal") | **RUB1-Semantikbruch** (P0-Klasse, Report 13): trainiert auf ta.macd(9/21/9), live mit `macd_dif_normal_12_26_9` unter demselben Feature-Namen gefüttert (13:153-154). |
| **OBV** | keine Engine-Spalte; ATS1 lokal 500-Kerzen-rebased (12:165-166) vs. Training roh ~300d kumuliert | erklärt die ATS1-Kalibrierungs-Inversion (Bucket 0,6–0,7 → 71% WR, 0,8–0,9 → 57%). |
| **Bollinger** | Engine ddof=1 vs. pandas_ta ddof=0 (14/18) | Bänder ~2,6% Differenz — klein, aber Cross-Pfad-Vergleiche verfälscht. |
| **HVN** | **vier unabhängige Definitionen**: Engine-Histogramm (bins=√n, Top-4) · trade_utils 60-Bins-Top-6 · strat_volume „exakter Float-Close" (degeneriert, P2.42) · `get_hvn_and_sr_levels` berechnet **trotz Namens gar kein HVN** (trade_utils.py:248-283) | „HVN" im Telegram-Text bedeutet je nach Bot etwas anderes; ein ATS1-Trade bekommt andere Level-Pools als ein ABR1-Trade auf demselben Chart (95d-Fenster ohne HVN vs. 1000h mit HVN/FVG). |

### D2 — Pivot-Erkennung: fünf Parameterwelten
argrelextrema order=**20** (Engine, trade_utils) · order=**5** (market_utils→16/17/21, QM 24 + Trainer, 22) · order=**10** (25 + smc-Trainer, ABR1 mit **edge-Padding und >=**) · `find_peaks distance=8` ohne Fenster-Dominanz (ATB 14) · rolling-Fenster 9 mit >= (Pattern-Detector 7). Dazu **Train/Live-Fensterbrüche**: QM live LIMIT 100 vs. Trainer 2 Jahre; TD/BB live LIMIT 150 vs. 2 Jahre; **TD-Pattern-Spannweite live ≤50 Kerzen vs. Training ≤100** (25:199 vs. smc_ml_trainer.py:123) — das Modell wurde auf einer breiteren Musterpopulation trainiert als live gefiltert wird.

### D3 — Normalisierungs-Chaos (Cross-Modell-Vergleiche verboten)
`atr_pct`: RUB1 0.01=1% vs. ATS/QM/SMC 1.0=1% (Faktor 100). Distanz-Vorzeichen: `ema_200_dist` positiv=„über EMA" bei MIS/ATS, **negativ**=„über EMA" bei QM/SMC/Master. Slope: vier inkompatible Skalen (Engine roh, ATS ×1000/close, ATB %/Tag, RUB per Timestamp-Regression). Innerhalb jedes Modells konsistent — aber jede Dashboard-/Orchestrator-Auswertung, die solche Features über Modelle hinweg vergleicht, ist faktorverfälscht.

- [ ] **K9 (MEDIUM): pandas_ta-Versions-Fragilität als tickende Uhr.** ABR1 (18:116-136) und ATB1 (14:188-193) hängen an exakten pandas_ta-Spaltennamen; bei ABR1 werden Mismatches still zu 0 (P0.12), bei ATB1 drückt ein KeyError jede Prediction auf 0.0 → **Bot verstummt ohne Alarm** (14:266-268). `requirements.txt` ist ungepinnt (P3.4) — ein `pip install -U pandas_ta` ändert stilles Verhalten. **Fix:** Prefix-Matching überall + Version pinnen + Startup-Assertion.
- [ ] **K10 (LOW): Engine-fillna-Politik uneinheitlich** (RSI→50, EMA/MA/BOLL→0, KAMA→NaN) — die 0-Fills sind die Wurzel von P1.13; KAMA zeigt die richtige Praxis.

---

## E. Priorisierte Empfehlungen

**Sofort (Stunden, kein Risiko):**
1. Indexe aus D1 anlegen (inkl. UNIQUE-Backstop auf `closed_ai_signals`) — größter Einzelhebel gegen die Scan-Last.
2. `VACUUM FULL telegram_outbox` + Partial-Index (D2); `wal_compression=on` (D3).
3. Müll droppen: 1.010 tote + 108 Quarterly-Tabellen, `pump_dump_events`-Retention (D5).

**Kurzfristig (Tage):**
4. K1 fixen (`model='ROM1'`-Filter + ORDER BY + ±60s-Fenster — deckt sich mit P1.8) und K2 entscheiden: **eine** Trailing-Semantik für beide Monitore (sonst bleibt jede Bot-Vergleichsstatistik verzerrt).
5. „Win"-Begriff vereinheitlichen (K3): eine zentrale `classify_outcome()` in core, von 23/27/Cooldowns benutzt.
6. Staleness-Gate (K7): Signal verwerfen/neu preisen, wenn Entry-Kerze älter als X min (pro Familie definiert).
7. pandas_ta pinnen + Prefix-Matching (K9).

**Mittelfristig (mit R1 zusammen planen):**
8. TimescaleDB-Migration auf 2 Hypertables (Abschnitt B) — löst Tabellen-Sprawl, WAL, autovacuum, Schema-Rollout und globales Querying strukturell; Dual-Write-Migration über `core/`-Helper.
9. Eine ATR-/RSI-Referenzimplementierung in `core/indicators.py`, alle lokalen Berechnungen darauf umstellen (D-Kernbefund); bei Retrains (Report 16, Abschnitt 8) konsequent den gemeinsamen Feature-Builder verwenden (X-R2).

**Einordnung:** Die Messungen bestätigen die Code-Vermutungen aus Report 02 quantitativ (Tabellen-Sprawl real: 9.782; WAL-Druck real: 8,5 GB/Tag; Upsert-Amplification real: 15×) und fügen zwei neue Klassen hinzu: die Index-Lücke auf den Auswertungs-Tabellen (D1 — reine Scan-Last von ~33 Mrd. Tupeln/Tag) und die Berechnungs-Inkonsistenzen (C/D), die erklären, **warum Bot-Vergleichsstatistiken derzeit nur eingeschränkt belastbar sind**: unterschiedliche Trailing-Semantik, drei Win-Definitionen, vier Entry-Frische-Klassen und drei ATR-Mathematiken fließen ungefiltert in dieselben Whitelist-/Ranking-Entscheidungen.
