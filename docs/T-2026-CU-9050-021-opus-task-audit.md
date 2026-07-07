# Opus Task-Audit Kythera — geranktes Backlog mit Reasoning

**Stand:** 2026-07-07 (Fable-5-Extraktion, T-2026-CU-9050-021). Quellen: `AUDIT_TODO.md` (126 Findings, 73 offen), KB-Tasks Projekt 9050, `audit_reports/`, CHANGELOG.
**Arbeitsregel:** in Ranking-Reihenfolge; pro Task erst das KB-Task-Doc lesen (dieser Stand ist die Interpretation vom 2026-07-07); Zyklus nach `docs/OPUS-HANDOFF.md` §2. Environment-Spalte beachten — **BUILD** = Build-Maschine reicht, **VPS** = braucht Live-DB/VPS-Session.

Übergeordnete Reihenfolge-Logik (aus dem Audit destilliert): **Root-Causes vor Punkt-Fixes** (R1–R4 erzeugen ~60% der Findings) · **Monitor-Korrektheit vor Modell-Neutraining** · **Z0 messen vor Perf-Fixen** · **Z2 (Tunnel) vor Z1 (Dashboard-Rewrite)**.

---

## Tier A — direkt ausführbar (keine Michi-Entscheidung nötig)

### A1 · Ledger- und KB-Hygiene (BUILD, ~1h, zuerst — schafft verlässliche Arbeitsgrundlage)
**Warum:** Fünf AUDIT_TODO-Checkboxen widersprechen ihrer eigenen Annotation (P1.5 ✘, P1.18 ✘, P1.26 ✘, P2.2 ✔, P2.50 ✔) und KB-Task T-2026-CU-9050-016 steht auf `open`, obwohl der Kern (Walk-Forward-Simulator, Trainer-Fixes, Batch-E-Reports 19/20) laut CHANGELOG geliefert ist. Ein falsches Ledger produziert falsche Priorisierung.
**Schritte:** (1) Pro Item die Annotation im Ledger verifizieren (nicht blind flippen), Checkbox + Datum setzen. (2) T-…-016 gegen CHANGELOG/Reports abgleichen: erledigten Teil dokumentieren, Rest (offene Replay-Adapter, siehe B7) sauber abgrenzen, KB-Status korrigieren oder Rest-Task anlegen.
**Done:** Ledger-Zählung stimmt mit Annotationen überein; 016 hat einen wahren Status.

### A2 · P1-Korrektheits-Batch Monitore/Tracker (BUILD, ~4-6h, datei-disjunkt parallelisierbar)
**Warum:** P1.43/P1.44 verfälschen die per-Bot-Statistik — die Entscheidungsbasis des Orchestrator-Gatings; P1.39/P1.41 fluten Alerts/Shadow-Zeilen; P1.11 schreibt leicht falsche "closed" Candles; P1.37 friert den Watchdog-Loop.
**Items + fixierter Plan (aus dem Ledger):**
- P1.43 Market-Tracker Pool-Leak: `try/finally close` + Rollback vor Fallback (`23_market_tracker.py:395-429,749-831`).
- P1.44 "opened"-Doppelzählung: `posted=TRUE`-Filter, Opens nur aus `ai_signals`+`closed_ai_signals` (`23:399-425`).
- P1.41 Shadow-Inserts ohne Cooldown: per-Symbol-Shadow-Cooldown; Konsumenten filtern `posted=TRUE` (`10:625-635`).
- P1.39 Pump/Dump Rest-Indexbasiertheit: über `_find_bucket_before/range` routen (`10:522-529,552-558`).
- P1.11 WS-Buffer-Key: `(sym,tf,open_time)` oder Flush bei `k['x']` (`1_data_ingestion.py:494-502`).
- P1.37 Watchdog-Backoff: per-Prozess `not_before`-Timestamp statt `time.sleep()` (`main_watchdog.py:299-303`).
**Done:** je Item Fix + betroffener `backtest/`-Test bzw. nachvollziehbarer Beweis im PR + Checkbox. **Achtung:** alles Geld-Pfad-nah — Qualitätsbar §5 "Code-Fix" voll anwenden.

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

### B5 · T-2026-CU-9050-010 Regression-Guard scharf schalten (VPS, ~1.5h, sobald VPS-Session existiert)
**Schritte stehen im KB-Task** (extract → `KYTHERA_GOLDEN_REFRESH=1 refresh` → status armed → Fixtures+Golden committen). **Warnungen aus dem Task:** Golden-Decay (jeder Tag Drift verschmutzt die Referenz — früh machen); Guard-Laufzeit <2-3s halten; Toleranzen nur spaltenweise mit dokumentiertem Grund weiten.

### B6 · T-2026-CU-9050-011 VPS-Port + Claude Code auf dem VPS (VPS/Ops, ~2.5h, **Blocker für B5, A4-Replay, B7**)
**Schritte stehen im KB-Task.** Harte Constraints: Live-Bot-Env nicht destabilisieren (kein pyarrow — Guard nutzt bewusst np.savez); `.env` mit echten Creds nie committen; Watchdog bleibt einziger Prozess-Owner. Offene Detailfrage im Task (eigene Checkout/Worktree-Isolation auf dem Live-Host) → mit Michi klären = einziger C-Anteil.

### B7 · P0.10-Rest: Replay-Adapter + Retrains für MIS1/QM/ATS1/ATB1/SRA1 (VPS, mehrere Sessions)
**Vorentscheid:** exakt dem Muster der gelieferten Adapter folgen (UFI1/TD/BB/ABR1, seit 07-06 auch RUB2/EPD2): Detection-Logik in geteilten `core/*`-Builder heben, `tools/walkforward_sim.py --strategy <s>`, Retrain via `retrain_from_replay.py`, Artefakt nach `staging_models/` mit neuem Model-Tag. **Rollout jedes Kandidaten = C-Gate (Michi).** Reihenfolge nach Live-Relevanz: MIS1 (→MIS2-Familie) zuerst, ATB1 zuletzt (geparkt).

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

## Empfohlene erste Opus-Woche

1. **A1** (Ledger wahr machen) → 2. **A2** (Monitor/Tracker-Korrektheit — direkte Verbesserung der Gating-Datenbasis) → 3. **B1** (R3, räumt das größte offene Cluster strukturell) → 4. **B6→B5** sobald Michi die VPS-Session freigibt (Guard-Decay drängt) → 5. **A3/B2/B3** nach Kapazität. C1/C3 nur als Vorbereitung, nie eigenmächtig.
