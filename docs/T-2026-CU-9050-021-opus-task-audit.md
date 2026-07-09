# Opus Task-Audit Kythera — geranktes Backlog mit Reasoning

**Stand:** 2026-07-09 (Ledger-Verifikation T-2026-CU-9050-028; Basis: Fable-5-Extraktion vom 2026-07-07, T-2026-CU-9050-021). Quellen: `AUDIT_TODO.md`, KB-Tasks Projekt 9050, `audit_reports/`, CHANGELOG.
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

---

## Tier A — direkt ausführbar (keine Michi-Entscheidung nötig)

### ~~A1 · Ledger- und KB-Hygiene~~ — **ERLEDIGT 2026-07-09 (T-2026-CU-9050-028)**
Fünf widersprüchliche Checkboxen am Code verifiziert statt geflippt. Ergebnis: P1.5/P1.11/P1.18/P2.50 geflippt; **P1.26 bleibt offen — die Annotation ✘ war selbst falsch**, der FVG-Entry ist realer Dead-Code (Mitigation-Scan und Trigger nutzen dasselbe Prädikat auf derselben Kerze); die 83 „beweisenden" Cooldown-Rows stammen aus einer älteren, TF-präfigierenden Codeversion, deren Key der aktuelle Code nie schreibt. P2.2 bleibt offen (TZ-Dimension gelöst, Spaltenbreite nicht — Live-`ALTER` ist Operator-Entscheid). T-2026-CU-9050-016 auf `done` mit abgegrenztem Rest-Scope korrigiert. Neu: P1.45, P2.51, P3.13.

### A2 · P1-Korrektheits-Batch Monitore/Tracker (BUILD, ~4-5h, datei-disjunkt parallelisierbar)
**Warum:** P1.43/P1.44 verfälschen die per-Bot-Statistik — die Entscheidungsbasis des Orchestrator-Gatings; P1.39/P1.41 fluten Alerts/Shadow-Zeilen; P1.37 friert den Watchdog-Loop.
**Restmenge am Code verifiziert (2026-07-09, T-028) — fünf von sechs Items offen.** Die PRs #13/#15 haben die Market-Tracker-Fläche angefasst, aber **keines** dieser Items miterledigt (ihre Dedup wirkt nur auf die geschlossenen Tabellen). Zeilennummern unten sind der Stand vom 07-09:
- **P1.43** Pool-Leak (OFFEN): zwei harte Leak-Sites — `23_market_tracker.py:418`/`:470` (`job_signal_summary`) und `:805`/`:904` (`job_per_bot_performance`) rufen `conn.close()` im try-Body direkt vor dem `except`. Dritte, marginale Site `_regime_conn` (`:1343`/`:1383`). Fehlender `rollback` bestätigt: `:892-894` feuert den Fallback-Query in der aborted Transaction. **Fix:** `try/finally close` + `rollback` vor Fallback.
- **P1.44** "opened"-Doppelzählung (OFFEN, orthogonal zu #13/#15): `get_o_stats` (`:615`) zählt aus vier konkatenierten Quellen (`:573`), darunter `ml_predictions_master` **ohne `posted`-Filter** (`:424`, Shadow zählt als Open) parallel zu `closed_ai_signals` (`:455`, Doppelzählung). Repo-weit kein `WHERE posted` in der Datei. **Fix:** `posted=TRUE`-Filter, Opens nur aus `ai_signals`+`closed_ai_signals`.
- **P1.41** Shadow-Inserts ohne Cooldown (OFFEN): das 900s-Gate (`10:583`) existiert, aber der Shadow-Branch (`10:651-661`) setzt `last_alert_time` nie zurück — der Reset steht nur im Live-Trade-Branch (`:750`). **Fix:** per-Symbol-Shadow-Cooldown; Konsumenten filtern `posted=TRUE`.
- **P1.39** Pump/Dump Rest-Indexbasiertheit (OFFEN): Bucket-Helfer existieren, werden aber nur im Price-Spike-Teil genutzt. Index-basiert geblieben: Volume-Explosion (`10:528,532`), ML-Features (`10:557,563`). **Fix:** über `_find_bucket_before/range` routen.
- **P1.37** Watchdog-Backoff (OFFEN): blockierendes `time.sleep(delay)` in `main_watchdog.py:443-446`; `not_before` existiert nirgends. **Fix:** per-Prozess `not_before`-Timestamp.
- ~~P1.11 WS-Buffer-Key~~ — **war bereits gefixt**, Key ist `(sym, tf, open_time)` (`1_data_ingestion.py:662`). Aus A2 gestrichen.
**Done:** je Item Fix + betroffener `backtest/`-Test bzw. nachvollziehbarer Beweis im PR + Checkbox. **Achtung:** alles Geld-Pfad-nah — Qualitätsbar §5 "Code-Fix" voll anwenden.

### A2b · P1.45 Artefakt-`model_id` in den Post-Pfaden verdrahten (BUILD, ~2-3h) — **vor jedem Retrain-Rollout**
`11_ai_mis`, `13_ai_rub` und `24_quasimodo` werfen die verfügbare `model_id` weg und posten unter einer Quellcode-Konstante (Details im Ledger, P1.45). Heute stimmt der Tag zufällig; beim nächsten Retrain (MIS3/RUB3/QM2) verschmelzen die Generationen still in Per-Bot-WR und Orchestrator-Gating — dieselbe Klasse, die PR #16 im Sniper gefixt hat. Muster: `18_ai_abr1_bot.py:520`. RUB richtungsabhängig fixen (LONG reused `"RUB2"` bewusst für ein Legacy-Modell). Je ein Guard-Test analog `backtest/test_sniper_tag.py`. **Blockiert B7 und C2.**

### A3 · P2-Robustheits-Cluster Ingestion/Housekeeping (BUILD, ~4h)
P2.14 (Retry-Bound + 418-Backoff), P2.16 (coins.json ein Writer, atomar — Filter-Parität ist seit dem ETHU-Vorfall heikel), P2.17 (Delisted-Cleanup nur Binance-Perp-Shape), P2.18 (Housekeeping-REST 429/418-Handling), P2.36/P2.37 (ATB1 unknown-state + Exception-Handling — Bot ist geparkt, Fix ist risikofrei), P2.49 (atomic_write_json Windows-Fix: unique tmp + retry).
**Done:** wie A2. Kein Live-Deploy nötig — Fixes wirken beim nächsten regulären Restart.

### A4 · T-2026-CU-9050-020 HMM-Regime-Studie (VPS für Replay-Daten, ~1 Tag, kein Live-Eingriff)
**Warum:** Alle ABR1-LONG-Fehlschläge hatten denselben Fehlermodus (Regime-Nichtstationarität). Studie ist billig falsifizierbar, Batch-E-Disziplin: Walk-Forward entscheidet.
**Schritte im KB-Task fixiert** (3–4-Zustands-Gauß-HMM auf BTC-4h; A/B gegen 26_regime_detector-Heuristik UND ROM1-Gating; Replay-Daten `_X/staging_models/replay/`). **Vorentscheid:** klassische Markov-Ketten auf Preiszuständen bewusst NICHT (Fees fressen Mikro-Edges). Ergebnis ist ein Report + Empfehlung, KEIN Live-Code.

### A5 · P3-Hygiene-Batch (BUILD, niedrig, Lückenfüller)
P3.1 (Dead-Code/Duplikate), P3.2 (Log-Rotation), P3.7 (Coin-Level-Exceptions auf ERROR+exc_info wie Bot 29), P3.8 (matplotlib `Agg` in 17/24/25), P3.10 (Spec-Drift-Doku). Nur mit Touch-Kontext oder als expliziter Batch — nicht als Drive-by in Geld-Pfad-PRs mischen.

## Tier B — fixierter Fable-Vorentscheid (ausführen; wenn die Realität abweicht → eskalieren statt improvisieren)

### B1 · R3 Zentrale UTC-Policy (BUILD, ~1 Tag, VOR dem P2-TZ-Cluster)
**Vorentscheid:** `core/time.py` mit `utc_now()`; `-c timezone=UTC` im Pool (`core/database.py`); Money-Zeitspalten → `timestamptz` (DDL-Wechsel nur repo-seitig vorbereiten, Live-`ALTER TABLE` ist Eskalation → C-Gate); ruff `DTZ`-Regeln aktivieren. Danach erst P2.1/P2.3–P2.6/P2.21 mechanisch nachziehen.
**Warum so:** Einzel-TZ-Fixes ohne zentrale Policy erzeugen neue Drift — das ist die dokumentierte Audit-Lektion. Step-2-Befund: die timestamptz-Variante hat live gewonnen (P2.2).

### B2 · R4 `cap_leverage_to_sl()` auf restliche Signal-Bots ausrollen (BUILD, ~2-3h)
**Vorentscheid:** existierende Funktion `core/trade_utils.py` verwenden (Muster: Bots 21/29/ROM1). Kein neues Konzept erfinden. Jeder Bot einzeln committen (Geld-Pfad, klein + kohäsiv).

### B3 · R2 Fleet-/Schema-Single-Source (BUILD, ~1 Tag)
**Vorentscheid:** `core/fleet.py` als eine Prozess-Definition (Watchdog + Dashboard konsumieren); `docs/schema.sql` als DDL-Referenz inkl. der bisher DDL-losen Tabellen (`ai_signals`, `ml_predictions_master`). Migration-Runner nur design-seitig — Live-Schema fasst B3 nicht an.

### B4 · P0.8/Z2 Dashboard-Absicherung: Cloudflare Tunnel VOR Dashboard-Rewrite (VPS, ~0.5-1 Tag)
**Vorentscheid:** Reihenfolge Z2 vor Z1 ist entschieden. Schritt 1 sofort machbar: `dashboard.py` auf `127.0.0.1` binden + `cloudflared` als Windows-Service (outbound-only) + Cloudflare Access. Das entschärft P0.8 (unauthentifiziertes `stop_all` auf 0.0.0.0:5000), ohne den Rewrite abzuwarten. Deployment-Moment (Dashboard-Neustart) mit Michi terminieren — der Bind-Wechsel selbst ist der einzige Live-Touch.

### ~~B5 · T-2026-CU-9050-010 Regression-Guard scharf schalten~~ — **BEREITS SCHARF** (Korrektur 2026-07-09, T-028)
Der Guard **ist armed**: 24 Goldens + 24 Fixtures + `manifest.json` sind seit Commit `4765e25` git-tracked, `verify` läuft als pre-commit-Hook bei jedem Commit (`.pre-commit-config.yaml:43`) und meldet „OK - 24 fixture(s) match the golden snapshot". Dieses Dokument und `OPUS-HANDOFF.md` §3 behaupteten bis heute das Gegenteil — wer danach priorisierte, hätte eine erledigte Arbeit wiederholt. **Offen bleibt nur P2.51:** `mode_verify` gibt bei fehlenden Goldens Exit 0 zurück, ohne auf ein existierendes Manifest zu prüfen → wer `golden/` löscht, disarmt den Guard still. Kleiner BUILD-Fix, kein VPS nötig. Die Task-Warnungen zu Golden-Decay und Toleranzen gelten weiter für künftige Refreshes.

### B6 · T-2026-CU-9050-011 VPS-Port + Claude Code auf dem VPS (VPS/Ops, ~2.5h, **Blocker für B5, A4-Replay, B7**)
**Schritte stehen im KB-Task.** Harte Constraints: Live-Bot-Env nicht destabilisieren (kein pyarrow — Guard nutzt bewusst np.savez); `.env` mit echten Creds nie committen; Watchdog bleibt einziger Prozess-Owner. Offene Detailfrage im Task (eigene Checkout/Worktree-Isolation auf dem Live-Host) → mit Michi klären = einziger C-Anteil.

### B7 · P0.10-Rest: Replay-Adapter + Retrains für QM/ATS1/ATB1/SRA1 (VPS, mehrere Sessions)
**Vorentscheid:** exakt dem Muster der gelieferten Adapter folgen: Detection-Logik in geteilten `core/*`-Builder heben, `tools/walkforward_sim.py --strategy <s>`, Retrain via `retrain_from_replay.py`, Artefakt nach `staging_models/` mit neuem Model-Tag. **Rollout jedes Kandidaten = C-Gate (Michi).**
**Korrektur 2026-07-09 (T-028): MIS1 gehört nicht mehr in diese Liste.** Adapter und Retrain-Code sind gebaut — `walkforward_sim.py` unterstützt `ufi1, td, bb, abr1, mis1, rub` (`:906`), `retrain_from_replay.py` zusätzlich `epd` (`:771`), geteilte Builder `core/{mis,rub,funding}_features.py` existieren. Bei MIS1 steht nur noch die **Ausführung** aus (400d-Replay auf dem VPS → MIS2-Familie trainieren → Kalibrierungs-Report), nicht der Code.
**Ohne jeden Adapter (Grep: 0 Treffer in `walkforward_sim.py`):** QM, ATS1, ATB1, SRA1. Reihenfolge nach Live-Relevanz: MIS1-Ausführung + QM zuerst, ATB1 zuletzt (geparkt).
**Vorbedingung: A2b (P1.45).** MIS und QM sind genau die Bots, die ihre Artefakt-`model_id` verwerfen — ein MIS3/QM2-Rollout vor dem Fix postet die neue Generation still unter dem alten Tag. Ausserdem: `retrain_from_replay.py:723` (EPD2) und `retrain_sra2.py:281` (SRA2) schreiben dict-Artefakte **mit** `model_id`, während die Live-Bots `10_pump_dump_detector` und `9_ai_sr_bot` rohe Modelle laden und keine Meta lesen — beim Verdrahten muss der Tag aus der `model_id` kommen.

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

## Empfohlene Reihenfolge (aktualisiert 2026-07-09 nach A1)

~~A1~~ erledigt. ~~B5~~ war bereits scharf. Von hier:

1. **A2** (Monitor/Tracker-Korrektheit — direkte Verbesserung der Gating-Datenbasis; Restmenge ist verifiziert, fünf Items)
2. **A2b** (P1.45 `model_id`-Verdrahtung — klein, aber **blockiert jeden Retrain-Rollout**; sinnvoll direkt nach oder parallel zu A2, da datei-disjunkt)
3. **B1** (R3 zentrale UTC-Policy — räumt das grösste offene Cluster strukturell)
4. **B6** sobald Michi die VPS-Session freigibt (Blocker für A4-Replay und B7)
5. **A3/B2/B3** nach Kapazität. Lückenfüller: **P2.51** (Guard-Disarm-Härtung, ~15 min BUILD), **P1.26** (SMC-FVG-Dead-Code, Einzeiler + Test).

C1/C3 nur als Vorbereitung, nie eigenmächtig. Kein Retrain-Rollout vor A2b.
