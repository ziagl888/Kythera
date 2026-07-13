# Design: R1 + TimescaleDB-Migration (Candles/Indicators → 2 Hypertables)

**Status:** Entwurf (2026-07-04) · **Autor:** Audit-Session · **Voraussetzung:** Fleet läuft stabil auf dem Stand nach Batch 4 + WS-Fixes.

**Ziel:** Ein Projekt, zwei Root-Causes:
1. **R1 (Audit #1):** Forming-Candle-Vertrag festnageln — Look-ahead/Repaint in ~allen Strategien und ML-Bots beenden. Voraussetzung für das gesamte Retrain-Programm (Report 16, Abschnitt 8).
2. **Tabellen-Sprawl (Report 18):** 9.297 per-Symbol-Tabellen → 2 Hypertables. Erwartete Effekte: Storage 25 GB → ~4–6 GB (Compression), WAL-Kollaps, autovacuum-Entlastung, globale Queries, Schema-Änderungen in einer Spalte statt 9.297 Rollouts.

---

## 1. Ziel-Schema

```sql
CREATE TABLE candles (
    symbol     text        NOT NULL,
    tf         text        NOT NULL,          -- '5m','15m','30m','1h','2h','4h','1d','1w'
    open_time  timestamptz NOT NULL,
    open       double precision,
    high       double precision,
    low        double precision,
    close      double precision,
    volume     double precision,
    is_closed  boolean     NOT NULL DEFAULT false,   -- R1: der Vertrag
    PRIMARY KEY (symbol, tf, open_time)
);
SELECT create_hypertable('candles', by_range('open_time', INTERVAL '7 days'));
CREATE INDEX ON candles (symbol, tf, open_time DESC);

ALTER TABLE candles SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, tf',
    timescaledb.compress_orderby   = 'open_time DESC'
);
SELECT add_compression_policy('candles', INTERVAL '14 days');
```

`indicators` analog: gleiche Keys + `is_closed` + die ~120 Indikator-Spalten. **Entscheidung dabei fällig (P3.12):** Spalten von `REAL` (float4) auf `double precision` heben — Sub-Cent-Coins verlieren aktuell Präzision; Compression macht den Größenunterschied nahezu irrelevant.

**Der R1-Vertrag konkret:**
- Ingestion schreibt jede WS-Kline mit `is_closed = k['x']` (das Closed-Flag liefert Binance mit — es wird heute schlicht ignoriert).
- REST-Catch-up/Gap-Filler schreiben `is_closed = true` (historische Kerzen sind per Definition geschlossen), außer der jüngsten Periode.
- **Alle Indikator-/Strategie-/ML-Reader konsumieren ausschließlich `is_closed = true`.** Nur Preis-Checks (Monitore 5/8, get_live_price-Fallback) dürfen explizit die forming Candle sehen.

## 2. Eine API statt 40 f-String-Stellen: `core/candles.py`

Die Migration wird für die Bots unsichtbar, indem ALLE Zugriffe vorher durch eine zentrale API laufen:

```python
read_candles(conn, symbol, tf, limit, include_forming=False)   # → DataFrame, ASC
read_indicators(conn, symbol, tf, limit, include_forming=False)
latest_open_time(conn, symbol, tf)                              # Catch-up-Resume
upsert_candles(conn, rows, closed: bool)                        # Ingestion/Filler
upsert_indicators(conn, df, symbol, tf)                         # Engine
```

- Phase A: Die API liest/schreibt die **alten** Tabellen (reine Umverdrahtung, kein Verhaltenswechsel — außer dem bewussten `include_forming`-Default `False`, der R1 bot-für-bot scharf schaltet).
- Phase C: Die API wird intern auf die Hypertable umgestellt — **ohne dass ein Bot angefasst wird.**

**Bekannte Call-Sites (Umverdrahtungs-Backlog, ~40):** `1_data_ingestion` (Flush, Catch-up, Snapshot) · `2_indicator_engine` (Read Candles/Write Indicators) · `6_housekeeping` (Gap-Filler, Delisted-Scan) · `3_detectors` + `strategies/*` (480×Indicators, get_live_price-Fallback) · Monitore `5`/`8` (5m-Polls — *include_forming=True*) · `chart_data_service` · AI-Bots `9,10,11,12,13,14,15,16,18,21,24,25,29` · `28` (ROM1 5m-Close) · `core/trade_utils` (2×), `core/market_utils` · Trainer `qm/smc_ml_trainer`, `fib_backtest` · `tools/regression_guard`.

## 3. Migrations-Phasen (jede mit Gate)

| Phase | Inhalt | Gate (muss grün sein, sonst Stopp) |
|---|---|---|
| **0. Prep** (~0,5 T) | Hypertables anlegen (leer); `core/candles.py` gegen ALTE Tabellen; Symbol-Whitelist-Validierung in `load_coins` (P3.3 — verhindert neue Müll-Tabellen-Klasse in der Hypertable) | Unit-Smoke: API-Reads byte-gleich zu Direkt-SQL |
| **1. Reader-Umverdrahtung** (~2–3 T) | Call-Sites auf die API umstellen, Reihenfolge nach Risiko: chart_data_service → strategies/3 → AI-Bots → Monitore (mit `include_forming=True`!) → Engine-Reads. R1 wird hier pro Bot wirksam (`include_forming=False`) | Regression-Guard nach jedem Block; Signal-Raten im 24h-Vergleich (R1 WIRD Raten senken — dokumentieren, nicht erschrecken) |
| **2. Dual-Write** (~1 T) | Ingestion + Engine + Gap-Filler schreiben ZUSÄTZLICH in die Hypertables (forward-only ab Aktivierung); einmaliger Backfill-Copy der Historie per `INSERT INTO candles SELECT ..., true FROM "{SYM}_{tf}"` (Batch-Skript, nachts) | Paritäts-Query: Row-Counts + max(open_time) + Stichproben-Checksummen alt vs. neu, pro TF |
| **3. Parität beobachten** (≥5–7 Tage) | Fleet liest weiter ALT, schreibt doppelt. Täglicher automatischer Paritäts-Report (Cron) | 0 Drift-Findings an 3 aufeinanderfolgenden Tagen |
| **4. Read-Cutover** (~0,5 T) | `core/candles.py` intern auf Hypertable umschalten (Feature-Flag `KYTHERA_CANDLES_SOURCE=hyper`), Fleet-Restart | 24h Betrieb: Health-Monitor grün, Signal-Raten ±erwartbar, Query-Zeiten via pg_stat_statements ≤ alt |
| **5. Cleanup** | Dual-Write aus; alte Tabellen erst **nach 7 weiteren Tagen** droppen (vorher pg_dump-Sicherung); Compression-/Retention-Policies aktiv; `open_time`-Einzelindexe entfallen mit den Tabellen | Restore-Test des Dumps; DB-Größe & WAL-Rate dokumentieren (Erwartung: −70–80%) |

**Rollback ist in jeder Phase trivial:** Bis Phase 4 liest die Fleet die alten Tabellen; der Cutover selbst ist ein Env-Flag + Restart zurück.

## 4. Risiken & Gegenmaßnahmen

| Risiko | Einschätzung | Gegenmaßnahme |
|---|---|---|
| **R1 senkt Signal-Raten** (Bots sehen die forming Candle nicht mehr — gewollt!) | Sicher eintretend; Classic-Strats feuern seltener, MIS/RUB/ATB-Verteilungen verschieben sich | Vorher kommunizieren; Shadow-Vergleich 1 Woche; Schwellen erst NACH Retrain neu tunen (Report 16) |
| Disk während Dual-Write (~+22 GB unkomprimiert) | C: hat ~160 GB frei | Compression-Policy ab Tag 1 auf Chunks >14 d; Backfill nachts in Batches |
| Upserts in komprimierte Chunks (neuer Coin lädt 730 d Historie) | selten, langsamer aber unterstützt | Backfill-Pfad dekomprimiert gezielt bzw. schreibt vor Policy-Greifen |
| psycopg2-`execute_values` mit neuem Conflict-Target | klein | in `core/candles.py` gekapselt + Unit-Test |
| Trainer/Backtests lesen Alt-Tabellen hart | mittel | Trainer-Reads in Phase 1 mit umverdrahten (Batch E hat die Loader gerade angefasst — koordinieren!) |
| Monitore brauchen die forming Candle (Preis-Checks) | Design-Fallstrick #1 | explizites `include_forming=True` NUR dort; Code-Review-Checkliste |
| Zwei Sessions arbeiten parallel am Repo | real (heute mehrfach) | Migration als EIN Branch mit klarem Owner; Phase 1 in kleinen Commits pro Bot-Block |

## 5. Operator-Entscheidungen — ENTSCHIEDEN (Michi, 2026-07-13)

Durabler Record: **D-2026-CLD-109** (KB). Diese vier gaten die C-Gate-Phasen 2–5.

1. **Retention:** **UNBEGRENZT.** Keine `add_retention_policy` — nur die Compression-Policy. Komprimiert ist die Vollhistorie unkritisch (~4–6 GB).
2. **REAL → double precision** (P3.12): **JA**, für ALLE ~120 Indikator-Spalten im Zuge des Schema-Neubaus. Sub-Cent-Coins verlieren unter `REAL` Präzision; Compression macht den Größenunterschied irrelevant.
3. **1d/1w:** **NUR REST/Catch-up, kein WS mehr.** Spart ~1.300 Streams (IP-Drossel-Risiko). WS bleibt für 5m–4h. Der Umbau sitzt in `1_data_ingestion` (Block 6 / Phase 2).
4. **Retrain:** **Alle möglichen Bots der Reihe nach rerunnen** (Sequential-Jobs-Regel, ein Job gleichzeitig). R1 (`include_forming=False`) verschiebt Feature-Verteilungen fleet-weit → jedes ML-Modell braucht Retrain auf R1-sauberen Walk-Forward-Labels; Artefakte nach `staging_models/`, Rollout je Bot Operator-Entscheidung. Prerequisit für indikator-abhängige Retrains: der historische Indikator-Recompute (T-061/P1.13) — der Backfill ist reiner Copy/Cast, kein Recompute, d.h. Alt-Indikatoren tragen den Forming-Kontaminationswert.

> **Startzeitpunkt der C-Gate:** frühestens nach fertiger Reader-Umverdrahtung (Blocks 3–6) und nach der T-061-Rerun-Queue; jeder irreversible Schritt (Hypertable-DDL, Backfill, Read-Cutover, Table-Drop) bleibt eskalations-gegatet (Michi).

## 6. Verifikations-Werkzeuge

- **Paritäts-Skript** `tools/candles_parity.py`: vergleicht pro (symbol, tf) Row-Count, max(open_time), Checksumme über OHLCV der letzten N Tage alt vs. neu; Exit ≠ 0 bei Drift → als Phase-3-Cron.
- **Regression-Guard** (tools/regression_guard): bestehende Goldens laufen unverändert gegen die API — Phase-1-Gate.
- **pg_stat_statements** (seit heute aktiv): Query-Zeiten vorher/nachher als harte Cutover-Metrik.
- **Health-Monitor:** DATA_STALE-Check bleibt der Live-Kanarienvogel; beim Cutover zusätzlich temporär auf 2 Symbole erweitern.
