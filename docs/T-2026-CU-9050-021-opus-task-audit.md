# Opus Task-Audit Kythera — geranktes Backlog mit Reasoning

**Stand:** 2026-07-11 (Orchestrierungs-Welle T-2026-CU-9050-075 nachgezogen — T-2026-CU-9050-094; davor Ledger-Verifikation T-028 vom 2026-07-09, Basis Fable-5-Extraktion vom 2026-07-07, T-021). Quellen: `AUDIT_TODO.md` (frisch gepflegt, Single Source of Truth), KB-Tasks Projekt 9050, `audit_reports/`, CHANGELOG.
**Arbeitsregel:** in Ranking-Reihenfolge; pro Task erst das KB-Task-Doc lesen (die KB kann weitergedreht sein als dieses Dokument); Zyklus nach `docs/OPUS-HANDOFF.md` §2. Environment-Spalte beachten — **BUILD** = Build-Maschine reicht, **VPS** = braucht Live-DB/VPS-Session. **Vor dem Priorisieren `git fetch`** — mehrere Sessions arbeiten am selben Repo (siehe Falle 15 im Handoff).

Übergeordnete Reihenfolge-Logik (aus dem Audit destilliert): **Root-Causes vor Punkt-Fixes** (R1–R4 erzeugen ~60% der Findings) · **Monitor-Korrektheit vor Modell-Neutraining** · **Z0 messen vor Perf-Fixen** · **Z2 (Tunnel) vor Z1 (Dashboard-Rewrite)**.

---

## Seit der Extraktion geliefert (2026-07-08/09)

Diese Arbeit ist gemergt und war in der 07-07-Fassung dieses Dokuments noch nicht sichtbar:

| Task | Datum | Ergebnis | PR |
|---|---|---|---|
| T-2026-CU-9050-022 | 07-08 | Doku-Politur, neue `docs/ARCHITECTURE.md` + README-Verlinkung | #11 |
| — | 07-08 | `tools/track_shadow_model.py` — read-only Shadow-Performance-Tracker für Modell-Tags | #12 |
| T-2026-CU-9050-023 | 07-09 | Market-Tracker Per-Bot-WR-Korrektheit: Dedup auf `closed_ai_signals`, `close_price>0`-Guard, Direction-Case-Normalisierung, kompakter A–Z-Model-Post | #13 |
| T-2026-CU-9050-024 | 07-09 | **Volume Indicator war seit 04.07. signal-tot** — `module_tag 'Volume Indicator'` sprengt `trade_cooldowns.module varchar(10)`, Fehler wurde geloggt und geschluckt. Atomare Cooldown-Writes + Length-Guard | #14 |
| T-2026-CU-9050-025 | 07-09 | Market-Tracker Dedup-Key v2 (Follow-up zu 023): `closed_ai_signals` 439.325 raw → Natural-Key kollabiert nur auf 360.682 → Report-14-Key `(symbol, model, direction, open_time)` zeigt 81.842 echte Trades. `DISTINCT ON` auf R14-Key, Survivor = frühester `close_time` | #15 |
| T-2026-CU-9050-026 | 07-09 | SMC-Sniper `send_cornix_signal` ignorierte die Artefakt-`model_id` — BB2/TD2-Trades posteten unter `BB_4H`/`TD_4H`. Verstoss gegen harte Regel 6. Generation-aware Orchestrator-Patterns | #16 |
| T-2026-CU-9050-028 | 07-09 | Dieses Dokument + `AUDIT_TODO.md` + `docs/OPUS-HANDOFF.md` gegen den Code verifiziert; T-016 auf `done` korrigiert; A2-Restmenge belegt statt geschätzt; drei neue Findings (P1.45, P2.51, P3.13) | — |

**Zwei neue Fehlerklassen aus 024/026 — beide fleet-weit gesweept (T-028):**
- *Stiller Signal-Tod durch Spalten-Overflow:* keine zweite aktive Instanz. Alle 18 `trade_cooldowns.module`-Writer aufgelöst, längster Tag 9 Zeichen, keine Trunkierungs-Kollision. Restrisiko als **P3.13** notiert.
- *Post-Pfad ignoriert Artefakt-`model_id`:* keine zweite **aktiv falsch feuernde** Instanz, aber **drei latente** — `11_ai_mis`, `13_ai_rub`, `24_quasimodo` werfen eine verfügbare `model_id` weg und posten unter einer Quellcode-Konstante. Notiert als **P1.45**, **blockiert MIS3/RUB3/QM2** (betrifft B7 und C2 direkt).

## Seit der Extraktion geliefert (2026-07-10 — Zwischenbatch)

Zwischen der 07-09-Fassung und der Orchestrierungs-Welle lief ein grösserer 07-10-Batch (Task-Details in den KB-Tasks + `AUDIT_TODO.md`-Annotationen). Kurz, weil AUDIT_TODO die Detailbeweise trägt:

- **A2 komplett geschlossen** (P1.37 Watchdog-Backoff, P1.39 Pump/Dump-Timestamp, P1.41 Shadow-Cooldown, P1.43 Tracker-Pool-Leak, P1.44 Opened-Doppelzählung) — T-2026-CU-9050-029, PRs #18/#23. P1.11 war ohnehin schon gefixt.
- **P1.26** SMC-16-FVG-Dead-Code (Einzeiler + Guard-Test) — T-033.
- **P1.45-Nebenbefund EPD2/SRA2** verdrahtet (die Live-Bots `10_pump_dump`/`9_ai_sr` lesen jetzt Tag+Threshold aus der Meta) — T-042 (PR #39) + der Feature-Paritäts-Rest.
- **Neue Fehlerklasse „AI-Bot ohne Active-Trade-Check" fleet-weit geschlossen** (P1.47 ATB1-`posted`-Flag T-062, P1.48 RUB T-043, P1.49 EPD+SRA + Funding-Cache T-055) — alle postenden AI-Bots haben jetzt den Positions-Guard.
- **P1.46** Sniper-Forming-Pivots (T-036) + **P1.13** Warm-up-`fillna`→NaN (T-045/054) und der Recompute-Werkzeug-Schritt (T-061) — der Live-Recompute + TD2/BB2/QM2-Retrain bleibt operator-gegatet.

## Orchestrierungs-Welle 2026-07-11 (T-2026-CU-9050-075-Dispatch, PRs #66–#80)

Der Orchestrator T-075 hat die Tag-Wellen 1–6 als datei-disjunkte BUILD-Tasks T-076..T-093 dispatcht. Gegen `AUDIT_TODO.md` gegengelesen — **Checkbox-Zustand ist massgeblich, nicht die Dispatch-Liste**:

| Task | Ergebnis | Findings |
|---|---|---|
| T-076 | Regression-Guard: Manifest-vorhanden-aber-Goldens-weg → Exit 1; Cooldown-Tag-Test um die MIS-Horizonte erweitert | P2.51, P3.13 |
| T-077 | DB-Pool `statement_timeout`(300s) + TCP-Keepalives + Watchdog-Log-Heartbeat (`check_heartbeat`, Auto-Restart default-OFF) | P2.47 |
| T-079 | `core/coins.py` — ein atomarer `coins.json`-Writer + Binance-USDT-Perp-Shape-Guard beim Delisted-Cleanup + Empty-Universe-Guard | P2.16, P2.17 |
| T-080 | Cornix-Double-Parse-Check 24/25/29 — **verifizierter No-op** (`parse_cornix_signal` → `None` je Bot; 06.07.-Fix present+korrekt) | P3.9 |
| T-081 | Market-Tracker `_hard_split_block`-Chunker (≤4096); Full-History-Load + synchrone „async"-Jobs als bekanntes Risiko dokumentiert | P2.41 (PR #69) |
| T-082 | Orchestrator `run_startup_reconciliation` + schreib-seitiger `cleanup_stale_whitelist_rows` (Rohname + 14d) | P2.24, P2.25 |
| T-083 | Monitore/`post_ai_signal` speichern exakt die publizierten Targets (3/5), Cornix-Block byte-identisch | P2.31 |
| T-084 | Fensterglobale Indikatoren (27 Spalten) nur auf die neueste GESCHLOSSENE Kerze (Variante B), forming+ältere NULL; 4 S/R-Reader auf `first_valid_index` | P1.12 |
| T-085 | Detector-Zyklus ein Batch-`ticker/price` statt ~530 seriell + Volume-Spike/HVN-Reklassifikation | P2.44, P2.42 |
| T-086 | ATB1 unknown-State = observe-only + Main-Loop-Härtung (wirkt beim Entparken) | P2.36, P2.37 |
| T-087 | Watchdog `CTRL_BREAK_EVENT` (graceful) + `atomic_write_json` Windows-Fix (unique tmp/retry) | P2.48, P2.49 |
| T-088 | `21_btc_smc` Cooldown/Dedupe (`BTCSMC_1H`, 12h) + Funding-„Extreme"-Schwelle 75→95/85 | P2.46, P2.40 |
| T-089 | SMC/Mayank/Sniper: Weekend-/Stale-Candle-Gate, FVG-Age-Limit (50 Bars), SL/RR-Guard, Break-and-Retest wählt echtes Level | P2.45, P2.39 |
| T-090 | AIM2 Kandidatenfenster 30→60 min + tabellen-agnostischer `conv_signal`-Identity-Dedup (shadow-only) | P2.35 |
| T-091 | `core/fleet.py` = eine Prozess-Definition; Watchdog + Dashboard importieren sie (Bots 26–34 jetzt sichtbar) | R2(a), P1.38-tlw. |
| T-093 | Sniper-Kanten-Pivots — `argrelextrema`-Rest-Repaint am rechten Rand (Option B, ≥5 bestätigende geschlossene Bars) | P1.46-Rest |

**Noch in Arbeit (Checkbox offen, nicht mit einbeziehen):**
- **T-092** Datenpipeline-Robustheit (P2.13 Gap-Continuity, P2.15 Coin-Refresh, P2.20 `chart_data_service`-Watchdog) — `in_progress`, die drei Boxen bleiben `[ ]`. Der T-075-Dispatch listete diese Punkte, geschlossen sind sie **nicht** — im Ledger als offen geführt.
- **T-078** (P1.12-Erstversuch) → **wontfix**, von T-084 (Variante B) abgelöst.
- **Nur teilweise:** **R2** (R2(a) Prozessliste erledigt, R2(b) `schema.sql`/DDL braucht VPS/DB) und **P1.38** (Prozesslisten-Drift via `core/fleet.py` weg; CSRF-Origin-Check, Log-Streaming-Handle und die `/api/status`-psutil-Sweeps bleiben offen).
- **Neue Bots/Prep (nicht Checkbox-schliessend):** MAX1 (`34_ai_max1_bot.py`, RUB2-SHORT-Klon, shadow-only, `MAX1_LIVE_POSTING`=AUS, T-067/070) und die QM2-Retrain-Vorbereitung (`qm_ml_trainer.py` schreibt jetzt `model_id`, T-061).

---

## Tier A — direkt ausführbar (keine Michi-Entscheidung nötig)

### ~~A1 · Ledger- und KB-Hygiene~~ — **ERLEDIGT 2026-07-09 (T-2026-CU-9050-028)**
Fünf widersprüchliche Checkboxen am Code verifiziert statt geflippt. Ergebnis: P1.5/P1.11/P1.18/P2.50 geflippt; **P1.26 blieb am 07-09 offen — die Annotation ✘ war selbst falsch**, der FVG-Entry ist realer Dead-Code (Mitigation-Scan und Trigger nutzen dasselbe Prädikat auf derselben Kerze); die 83 „beweisenden" Cooldown-Rows stammen aus einer älteren, TF-präfigierenden Codeversion, deren Key der aktuelle Code nie schreibt (inzwischen erledigt, 2026-07-10 T-033). P2.2 bleibt offen (TZ-Dimension gelöst, Spaltenbreite nicht — Live-`ALTER` ist Operator-Entscheid). T-2026-CU-9050-016 auf `done` mit abgegrenztem Rest-Scope korrigiert. Neu: P1.45, P2.51, P3.13.

### ~~A2 · P1-Korrektheits-Batch Monitore/Tracker~~ — **ERLEDIGT 2026-07-10 (T-2026-CU-9050-029, PRs #18/#23)**
Alle fünf offenen Items geschlossen: P1.43 (Tracker-Pool-Leak `try/finally` + `rollback`), P1.44 (Opened-Doppelzählung: Opens nur aus `ai_signals`∪`closed_ai_signals`, `posted=TRUE`-JOIN), P1.41 (Shadow-Cooldown via `log_prediction`), P1.39 (Pump/Dump-Timestamp auf Bucket-Helfer + `_find_bucket_nearest`-Nachtrag T-035), P1.37 (Watchdog-`not_before` statt `time.sleep`). P1.11 war ohnehin schon gefixt. Je Item DB-freier `backtest/`-Guard. Damit ist die Gating-Datenbasis (per-Bot-Statistik) korrekt.

### ✅ A2b · P1.45 Artefakt-`model_id` in den Post-Pfaden verdrahten (BUILD, ~2-3h) — **erledigt 2026-07-09, T-2026-CU-9050-030 / PR #24**
**War:** `11_ai_mis`, `13_ai_rub` und `24_quasimodo` warfen die verfügbare `model_id` weg und posteten unter einer Quellcode-Konstante (Details im Ledger, P1.45) — dieselbe Klasse, die PR #16 im Sniper gefixt hat.

**Erledigt:** MIS zieht die Generation je Horizont aus `meta.model_id`, RUB richtungsabhängig (SHORT aus der Meta, LONG behält die benannte Konstante `RUB_LONG_TAG` für sein Legacy-Modell), QM präventiv inkl. `module_tag` als Pflicht-Keyword in `send_cornix_signal`. Dazu der transitionale Dedup, weil der Tag zugleich der Dedupe-Key ist (MIS/QM: `model IN (neu, legacy)`; RUB hat keinen Active-Trade-Check → Cooldown gegen beide Tags). Drei mutations-geprüfte Guard-Tests. Keine Live-Semantik-Änderung — die Tags bleiben heute MIS2-\*, RUB2, QM_1H. **B7 und C2 sind damit entblockt.** Der EPD2/SRA2-Nebenbefund ist inzwischen ebenfalls erledigt (2026-07-10, T-042/PR #39 verdrahtet Tag+Threshold aus der Meta, T-055 ergänzt den Active-Trade-Check + Funding-Cache) — der EPD2/SRA2-Rollout bleibt Operator-Entscheid (C-Gate).

### ~~A3 · P2-Robustheits-Cluster Ingestion/Housekeeping~~ — **ERLEDIGT (07-09 bis 07-11)**
P2.14/P2.18 (Retry-Bound + 429/418-Backoff, `core/http_retry.py`, T-2026-CU-9050 07-09-Batch), P2.16/P2.17 (`core/coins.py` ein atomarer Writer + Perp-Shape-Guard, T-079), P2.36/P2.37 (ATB1 unknown-state observe-only + Main-Loop-Härtung, T-086), P2.49 (`atomic_write_json` Windows-Fix, T-087). Alle Fixes wirken beim nächsten regulären Restart, kein Live-Deploy.

### A4 · T-2026-CU-9050-020 HMM-Regime-Studie (VPS für Replay-Daten, ~1 Tag, kein Live-Eingriff)
**Warum:** Alle ABR1-LONG-Fehlschläge hatten denselben Fehlermodus (Regime-Nichtstationarität). Studie ist billig falsifizierbar, Batch-E-Disziplin: Walk-Forward entscheidet.
**Schritte im KB-Task fixiert** (3–4-Zustands-Gauß-HMM auf BTC-4h; A/B gegen 26_regime_detector-Heuristik UND ROM1-Gating; Replay-Daten `_X/staging_models/replay/`). **Vorentscheid:** klassische Markov-Ketten auf Preiszuständen bewusst NICHT (Fees fressen Mikro-Edges). Ergebnis ist ein Report + Empfehlung, KEIN Live-Code.

### A5 · P3-Hygiene-Batch (BUILD, niedrig, Lückenfüller) — **überwiegend offen**
Bereits geschlossen: **P3.13** (Cooldown-Tag-Längennetz um die MIS-Horizonte erweitert, T-076) und der `db_schema_analysis.py`-Teil von **P3.1** (gelöscht, T-039). Offen bleiben P3.1-Rest (Dead-Code/`load_coins`-Dup), P3.2 (Log-Rotation), P3.3-P3.6, P3.7 (Coin-Level-Exceptions auf ERROR+exc_info wie Bot 29), P3.8 (matplotlib `Agg` in 17/24/25), P3.10 (Spec-Drift-Doku), P3.11, P3.12 (`REAL`→`double`, DB). Nur mit Touch-Kontext oder als expliziter Batch — nicht als Drive-by in Geld-Pfad-PRs mischen.

## Tier B — fixierter Fable-Vorentscheid (ausführen; wenn die Realität abweicht → eskalieren statt improvisieren)

### B1 · R3 Zentrale UTC-Policy (BUILD, ~1 Tag, VOR dem P2-TZ-Cluster)
**Vorentscheid:** `core/time.py` mit `utc_now()`; `-c timezone=UTC` im Pool (`core/database.py`); Money-Zeitspalten → `timestamptz` (DDL-Wechsel nur repo-seitig vorbereiten, Live-`ALTER TABLE` ist Eskalation → C-Gate); ruff `DTZ`-Regeln aktivieren. Danach erst P2.1/P2.3–P2.6/P2.21 mechanisch nachziehen.
**Warum so:** Einzel-TZ-Fixes ohne zentrale Policy erzeugen neue Drift — das ist die dokumentierte Audit-Lektion. Step-2-Befund: die timestamptz-Variante hat live gewonnen (P2.2).

### B2 · R4 `cap_leverage_to_sl()` auf restliche Signal-Bots ausrollen (BUILD, ~2-3h)
**Vorentscheid:** existierende Funktion `core/trade_utils.py` verwenden (Muster: Bots 21/29/ROM1). Kein neues Konzept erfinden. Jeder Bot einzeln committen (Geld-Pfad, klein + kohäsiv).

### B3 · R2 Fleet-/Schema-Single-Source (BUILD) — **R2(a) erledigt, R2(b) offen**
**R2(a) erledigt 2026-07-11 (T-2026-CU-9050-091):** `core/fleet.py` ist die eine Prozess-Definition (Name/Script/Group/Delays); `main_watchdog.py` + `dashboard.py` importieren sie — Drift geschlossen, das Dashboard zeigt automatisch die volle Fleet inkl. Bots 26–34, per `backtest/test_fleet_definition.py` festgenagelt, keine Verhaltensänderung am Watchdog. Miterledigt: der Prozesslisten-Teil von **P1.38** (CSRF/Log-Streaming/`/api/status`-Perf bleiben offen).
**R2(b) offen (VPS/DB):** `docs/schema.sql` als DDL-Referenz inkl. der bisher DDL-losen Tabellen (`ai_signals`, `ml_predictions_master`); `trade_cooldowns`-Breiten-Drift (`varchar(10)` live, siehe P2.2). Migration-Runner nur design-seitig — Live-Schema fasst R2(b) nicht an.

### B4 · P0.8/Z2 Dashboard-Absicherung: Cloudflare Tunnel VOR Dashboard-Rewrite (VPS, ~0.5-1 Tag)
**Vorentscheid:** Reihenfolge Z2 vor Z1 ist entschieden. Schritt 1 sofort machbar: `dashboard.py` auf `127.0.0.1` binden + `cloudflared` als Windows-Service (outbound-only) + Cloudflare Access. Das entschärft P0.8 (unauthentifiziertes `stop_all` auf 0.0.0.0:5000), ohne den Rewrite abzuwarten. Deployment-Moment (Dashboard-Neustart) mit Michi terminieren — der Bind-Wechsel selbst ist der einzige Live-Touch.

### ~~B5 · T-2026-CU-9050-010 Regression-Guard scharf schalten~~ — **BEREITS SCHARF** (Korrektur 2026-07-09, T-028)
Der Guard **ist armed**: 24 Goldens + 24 Fixtures + `manifest.json` sind seit Commit `4765e25` git-tracked, `verify` läuft als pre-commit-Hook bei jedem Commit (`.pre-commit-config.yaml:43`) und meldet „OK - 24 fixture(s) match the golden snapshot". Dieses Dokument und `OPUS-HANDOFF.md` §3 behaupteten bis heute das Gegenteil — wer danach priorisierte, hätte eine erledigte Arbeit wiederholt. **P2.51 erledigt 2026-07-11 (T-2026-CU-9050-076):** `mode_verify` prüft jetzt `os.path.exists(MANIFEST_PATH)` im leeren-Goldens-Zweig → Manifest-vorhanden-aber-Goldens-weg gibt **Exit 1** statt still Pass; Manifest-abwesend bleibt der legitime Pre-Live-DB-Freeze-Pass (`backtest/test_regression_guard_disarm.py`). Damit ist B5 vollständig zu. Die Task-Warnungen zu Golden-Decay und Toleranzen gelten weiter für künftige Refreshes.

### B6 · T-2026-CU-9050-011 VPS-Port + Claude Code auf dem VPS (VPS/Ops, ~2.5h, **Blocker für A4-Replay, B7**)
**Schritte stehen im KB-Task.** Harte Constraints: Live-Bot-Env nicht destabilisieren (kein pyarrow — Guard nutzt bewusst np.savez); `.env` mit echten Creds nie committen; Watchdog bleibt einziger Prozess-Owner. Offene Detailfrage im Task (eigene Checkout/Worktree-Isolation auf dem Live-Host) → mit Michi klären = einziger C-Anteil.

### B7 · P0.10-Rest: Replay-Adapter + Retrains für QM/ATS1/ATB1/SRA1 (VPS, mehrere Sessions)
**Vorentscheid:** exakt dem Muster der gelieferten Adapter folgen: Detection-Logik in geteilten `core/*`-Builder heben, `tools/walkforward_sim.py --strategy <s>`, Retrain via `retrain_from_replay.py`, Artefakt nach `staging_models/` mit neuem Model-Tag. **Rollout jedes Kandidaten = C-Gate (Michi).**
**Korrektur 2026-07-09 (T-028): MIS1 gehört nicht mehr in diese Liste.** Adapter und Retrain-Code sind gebaut — `walkforward_sim.py` unterstützt `ufi1, td, bb, abr1, mis1, rub` (`:906`), `retrain_from_replay.py` zusätzlich `epd` (`:771`), geteilte Builder `core/{mis,rub,funding}_features.py` existieren. Bei MIS1 steht nur noch die **Ausführung** aus (400d-Replay auf dem VPS → MIS2-Familie trainieren → Kalibrierungs-Report), nicht der Code.
**Ohne jeden Adapter (Grep: 0 Treffer in `walkforward_sim.py`):** QM, ATS1, ATB1, SRA1. Reihenfolge nach Live-Relevanz: MIS1-Ausführung + QM zuerst, ATB1 zuletzt (geparkt).
**Vorbedingung: A2b (P1.45) — ✅ vollständig erfüllt.** MIS/QM (T-030) sowie EPD2/SRA2 (T-042/T-055) lesen ihre Artefakt-`model_id` jetzt; ein MIS3/QM2/EPD3/SRA2-Rollout postet unter dem neuen Tag. Die `model_id`-Verdrahtung ist damit fleet-weit geschlossen — die Rollout-Sperre besteht nur noch als C-Gate (Michi), nicht mehr code-seitig. QM2-Retrain-Vorbereitung läuft (`qm_ml_trainer.py` schreibt `model_id`, T-061); der P1.13-Recompute davor bleibt operator-gegatet.

## Tier C — Michi-gated (Opus bereitet Briefing/Zahlen vor, ersetzt das Verdict nicht)

### C1 · T-2026-CU-9050-018 TimescaleDB-R1-Migration (VPS, ~14h, das große Strukturprojekt)
Design fertig (`docs/TIMESCALE_R1_MIGRATION.md`). **Offene Operator-Entscheidungen stehen im Task** (Retention, REAL→double, 1d/1w per REST, Startzeitpunkt) + Gate "3-5 Tage stabile Fleet". Opus darf vorbereiten: `core/candles.py`-API + Call-Site-Inventar (~40 Stellen) + `tools/candles_parity.py` als Code im Worktree. Dual-Write/Backfill/Cutover nur nach Go. **Warnung aus dem Task an Michi wiederholen:** R1 SENKT Signal-Raten (gewollt) — Schwellen erst nach Retrain neu tunen.
### C2 · Retrain-Rollouts / Artifact-Promotions (P0.12 ABR2-Kandidat, künftige B7-Kandidaten)
Opus liefert: Kalibrierungs-Report, Replay-PnL-Vergleich alt/neu, Empfehlung. Michi entscheidet Promotion. **Fixierter Negativ-Entscheid, nicht wieder aufmachen:** AIM1/P0.13 bleibt AUS, kein Vokabular-Retrain (Begründung: OPUS-HANDOFF §8).
### C3 · Z0 CPU-Grundlast-Programm (VPS; "WICHTIGSTER PUNKT" im Ledger)
Vorentscheid: **erst messen, dann fixen** — 10-min-Sampling per-Prozess gegen die bekannten Kandidaten (Full-Table-Scans D1, WAL/Tabellen-Sprawl, P2.19 WMA/KAMA, P2.44 538 HTTP-Calls, P1.40, P1.38). Opus darf die Messung bauen/laufen lassen (read-only); jede Maßnahme daraus, die Live-DB/Fleet berührt (D1-Indexe, D2 VACUUM FULL, D4, D5-Drops), einzeln zu Michi. Ziel: Grundlast <50%.
### C4 · Z1 Dashboard-Rewrite
Erst nach Z2 (B4). Tech-Entscheidung (Flask vs FastAPI+HTMX/React, SSE/WS, Mobile) ist eine Council-/Michi-Entscheidung — Opus bereitet die Optionen-Matrix vor, faltet P0.8/R2/P1.38/CSRF ein.

---

## Empfohlene Reihenfolge (aktualisiert 2026-07-11 nach der T-075-Welle)

**Fast der gesamte BUILD-Backlog ist abgearbeitet.** Erledigt: ~~A1~~, ~~A2~~, ~~A2b~~ (inkl. EPD2/SRA2), ~~A3~~, ~~B5~~, der grösste Teil des P2-Robustheits-Clusters (P2.14–P2.51 bis auf die unten genannten) und ~~R2(a)~~. Was jetzt noch offen ist, zerfällt in drei Gruppen:

**1. BUILD, ohne Michi ausführbar (Rest):**
- **T-092-Abschluss** — Datenpipeline-Robustheit **P2.13** (Gap-Continuity-Check), **P2.15** (Coin-Refresh ohne Restart), **P2.20** (`chart_data_service`-Watchdog + async Snapshot). Task ist `in_progress`; hier ansetzen, sobald es frei ist.
- **P2.12** (Wilder-RSI-Migration) — bewusste Migration mit Retrain-Kopplung, **erst nach T-092** anfassen (gemeinsame Indikator-Engine-Fläche).
- **B1 · R3 zentrale UTC-Policy** (`core/time.py` liegt, T-032) — der Pool-Flip auf `timezone=UTC` + der **TZ-Cluster P2.1–P2.6/P2.21** hängen daran; eigener Task mit Fleet-Restart-Fenster (Details `docs/UTC_POLICY.md`).
- **B2 · R4-Rest** (`cap_leverage_to_sl()` auf die restlichen signal-emittierenden Bots), **R1** (Forming-Candle-Vertrag repo-weit — teils via C1-Call-Site-Inventar), **P2.22/P2.23** (Regime-Attribution/„Unreliable"-Heuristik), **P2.38** (ABR1 `SUCCESS_CLASS_IDX`), **P3.12** (`REAL`→`double`, DB-nah).
- **A5 · P3-Batch** (P3.1-Rest, P3.2/P3.3/P3.5/P3.7/P3.8/P3.10/P3.11) — Lückenfüller, nicht in Geld-Pfad-PRs mischen.

**2. VPS/Ops — braucht Live-Host, Michi gibt die Session frei:**
- **B6** VPS-Port + Claude Code auf dem VPS (Blocker für A4-Replay und B7).
- **B4/Z2** Dashboard auf `127.0.0.1` + Cloudflare Tunnel (entschärft P0.8), **P0.7-Rest** (5 aktive Korrupt-Trades bereinigen), **P2.2-ALTER** (`module`-Spaltenbreite live), **R2(b)** `schema.sql`.
- **B7** Replay-Adapter + Retrains QM/ATS1/ATB1/SRA1; MIS1-Ausführung.

**3. Michi-gated (C-Gate) — Opus bereitet nur Briefing/Zahlen vor:**
- **C1** TimescaleDB-R1-Migration, **C2** Retrain-Rollouts/Artifact-Promotions (P0.12-ABR2, künftige B7-Kandidaten; AIM1/P0.13 bleibt AUS), **C3/Z0** CPU-Grundlast-Programm (erst messen), **C4/Z1** Dashboard-Rewrite (nach Z2).

C1/C3 nur als Vorbereitung, nie eigenmächtig. Die `model_id`-Rollout-Sperre (MIS3/RUB3/QM2/EPD3/SRA2) ist code-seitig aufgehoben — offen ist nur noch das jeweilige C-Gate.
