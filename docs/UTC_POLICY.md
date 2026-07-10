# UTC-Policy (R3)

**Stand:** 2026-07-09 · **Task:** T-2026-CU-9050-032 · **Root-Cause:** R3 (Audit) · **Cluster:** AUDIT_TODO P2.1–P2.6, P2.21

Kythera soll exakt eine Zeitdomäne haben: **UTC**. Diese Datei sagt, was davon jetzt gilt, was noch nicht gilt und in welcher Reihenfolge der Rest kommen muss. Sie ist der Handoff für den R3-Restart-Task.

---

## 1. Was jetzt gilt

| Ebene | Mechanismus | Datei | Status |
|---|---|---|---|
| Python | `utc_now()` / `utc_now_naive()` / `to_utc()` / `as_naive_utc()` / `from_unix_ts()` | `core/time.py` | **aktiv** |
| Lint | `ruff`-Regelgruppe `DTZ` verbietet naive `datetime.now()` / `utcnow()` / `fromtimestamp(ts)` | `pyproject.toml` | **aktiv** |
| Postgres | jede Pool-Session mit `-c timezone=UTC` | `core/database.py` | **offen** (siehe §4) |

Neuer Code kann ab sofort keine naive Lokalzeit mehr einführen, ohne dass CI rot wird. Bestehende bewusste Ausnahmen tragen ein `# noqa: DTZ…` mit Begründung — das ist die sichtbare Rest-Schuld, kein Freibrief:

- `3_detectors.py` — schreibt naive Lokalzeit in `active_trades_master.time/posted` (P2.3). Gehört zum Flip, siehe §4.
- `30_ai_pex1_bot.py` — Watermark-Sentinel gegen die naive Spalte `pump_dump_events.spike_time`. Bleibt dauerhaft naiv, das ist korrekt.

Die ruff-Excludes (`backtest/`, `tools/`, `strategies/`, `handlers/`, `trainers_x/`, `legacy_trainers/`) sind von DTZ nicht erfasst.

## 2. Warum die Session-TZ der Kern des Problems ist

Ein Teil der Live-Tabellen ist `TIMESTAMP WITHOUT TIME ZONE`. Postgres castet zwischen `timestamptz` und diesen naiven Spalten mit der **Session-TZ**. Damit hängt an der OS-TZ des VPS, was `NOW()` in eine naive Spalte schreibt und wie eine naive Spalte gegen `NOW()` verglichen wird.

**Der Offset ist +2/+3 h.** Die VPS-TZ ist `Europe/Bucharest` (EET/EEST), vermessen am 2026-07-05 (`tools/research_dataset_common.py:34`). Die AUDIT_TODO-Einträge P2.1–P2.6 sprechen von „CEST" und „1–2 h" — das ist die Grössenordnung, nicht die Zahl.

**Nicht jede naive Spalte trägt Lokalzeit.** Der Domänen-Unterschied hängt am Writer, nicht am Spaltentyp:

- Ein **naiver** Python-Parameter geht ungecastet durch — `26_regime_detector.py:216` schreibt `datetime.now(timezone.utc).replace(tzinfo=None)`, also naiv-**UTC**. Der ganze `regime_*`-Cluster ist heute schon korrekt und braucht **keine** Kompensation. Der Flip fasst ihn nicht an.
- Ein **aware** Parameter oder `NOW()` wird beim Schreiben in eine naive Spalte mit der Session-TZ gecastet und landet damit als **Lokalzeit** (`5_trade_monitor.posted`, `ml_predictions_master.time`, `pump_dump_events.spike_time`).
- `3_detectors.py` schreibt naive **Lokalzeit** direkt (P2.3).

Genau die zweite und dritte Gruppe kompensiert der Bestand bereits explizit (§5). Ein isolierter Fix macht diese Kompensationen falsch. Daran sind die Einzel-Fixes des Audits gescheitert.

## 3. Spalten-Inventar

Zielzustand ist überall `timestamptz`.

| Tabelle | Spalten | Ist | Bootstrap-DDL |
|---|---|---|---|
| `active_trades_master` | `time`, `posted` | naiv | `3_detectors.py` |
| `closed_trades_master` | `time`, `posted` | naiv | `5_trade_monitor.py` |
| `trade_cooldowns` | `last_posted_at` | **live `timestamptz`**, Repo-DDLs uneinheitlich (P2.2) | `26`, `11`, `24`, `25` |
| `regime_history` | `ts` | naiv | `26_regime_detector.py` |
| `regime_current` | `since`, `alt_context_since`, `last_raw_ts` | naiv | `26_regime_detector.py` |
| `bot_regime_performance` | `last_computed` | naiv | `26_regime_detector.py` |
| `bot_regime_whitelist` | `computed_at` | naiv | `26_regime_detector.py` |
| `orchestrator_open_trades` | `opened_at`, `closed_at` | naiv | `26_regime_detector.py` |
| `orchestrator_suppressed_signals` | `ts` | naiv | `26_regime_detector.py` |
| `pump_dump_events` | `spike_time` | naiv | `10_pump_dump_detector.py` |
| `ml_predictions_master` | `time` | naiv, **keine Repo-DDL** | — (Lücke, R2/B3) |
| `ai_signals` | `open_time` | **gemischte Domäne** — live verifiziert 2026-07-10 (T-044): Spalte ist `timestamp without time zone DEFAULT now()`, d. h. alle Writer, die `open_time` dem Default überlassen, stempeln Session-lokal (Bucharest). Ausnahme seit T-052: ROM1-Rows (`28_signal_orchestrator.insert_rom1_signal`) schreiben explizit naiv-UTC, damit der Lifecycle-Sync gegen das naiv-UTC `opened_at` matchen kann. Vereinheitlichung = R3-Flip (§4) | `28` (UTC), alle anderen AI-Bots (Default = lokal) |
| `closed_ai_signals` | `close_time` | bereits `timestamptz` | `8_ai_trade_monitor.py:27` |
| `{sym}_{tf}`, `ticker_10s`, `ml_predictions_master.processed_at` | `open_time`, `ts`, `processed_at` | bereits `timestamptz` | — |

## 4. Der offene Flip — was er anfasst und warum er ein eigener Task ist

`-c timezone=UTC` im Pool ist **kein Einzeiler**. Er verschiebt in einem Schlag die Domäne jeder naiven Spalte, die einen aware-UTC-Wert oder `NOW()` entgegennimmt, und muss deshalb zusammen mit allen abhängigen Stellen und einem Fleet-Restart-Fenster landen. Bestandteile:

1. `core/database.py` — `options="-c lock_timeout=30000 -c timezone=UTC"`.
2. `3_detectors.py` — beide `datetime.now()` auf `utc_now_naive()` (P2.3). **Pflicht**: ohne diesen Fix kippt der Flip `33_ai_fif1_bot.fifo_burst_counts` von korrekt auf Drift, während er `5_trade_monitor` (P2.6) und `core/market_utils.update_cooldown` (P2.5) repariert.
3. Die Kompensationen aus §5 entfernen.
4. Die Docstrings mitziehen, die heute „PG-Lokalzeit" behaupten: Modul-Docstring und `to_utc_naive()` in `15_ai_master_bot.py`, `fetch_recent_signals()` und `fifo_burst_counts()` in `33_ai_fif1_bot.py`, sowie die Header in `tools/*`.
5. Entscheiden, was mit der **Historie** passiert (§6).

Restart-Effekt: Zeilen von vor dem Restart tragen Lokalzeit und werden ab dann als UTC gelesen — sie erscheinen +2/+3 h in der Zukunft. Betroffen sind die kurzen Fenster (60 min Trade-Monitor, 1 h / 24 h FIF1-Burst-Dichte, 5 Tage AIM2-Signal-Stream); der Effekt läuft mit dem längsten Fenster aus.

`30_ai_pex1_bot.detect_spike_time_offset_h` misst seinen Offset zur Laufzeit gegen die Wanduhr und heilt sich nach dem Flip von selbst. Kein Eingriff nötig.

## 5. Die Kompensationen — der eigentliche Grund für den Zuschnitt

Sechs Stellen rechnen die Drift bereits explizit heraus. Sie sind heute **korrekt** und werden durch die Umstellung **falsch**.

Präzise: die Pool-Option **allein** fasst sie nicht an — sie vergleichen naive Parameter gegen naive Spalten, und das ist session-unabhängig. Falsch werden sie in dem Moment, in dem die **Writer** UTC schreiben (P2.3 und der aware-Cast unter UTC-Session). Da Flip und Writer-Fix zwingend zusammen landen (§4.2), ist das dieselbe Umstellung.

| Stelle | Was sie tut |
|---|---|
| `15_ai_master_bot.to_utc_naive()` + `load_signal_stream.since_local` | AIM2-Signal-Stream: `ml_predictions_master.time` und `*_trades_master.time` von Bukarest nach UTC |
| `tools/research_dataset_common.py` — `LOCAL_TZ` + `to_utc_naive()` | die geteilte Basis aller Research-Datasets |
| `tools/aim2_build_dataset.to_utc_naive()` | AIM2-Trainings-Datensatz |
| `tools/fif1_build_dataset.py` (importiert `to_utc_naive`) | FIF1-Trainings-Datensatz |
| `tools/pex1_build_dataset.py` (importiert `LOCAL_TZ`) | PEX1-Trainings-Datensatz |
| `tools/retrain_sra2.py` (lokalisiert `closed_trades3`-Zeiten) | SRA2-Retrain |

Die Trainer sind der harte Teil: sie lesen **Historie**. Nach dem Flip enthält jede naive Spalte beide Domänen — Lokalzeit vor dem Restart, UTC danach. Weder „immer kompensieren" noch „nie kompensieren" ist dann richtig. Ein Trainer, der das ignoriert, produziert Train/Serve-Skew — genau den Fehlermodus, gegen den AIM2 gebaut wurde (P0.13).

## 6. Historie: Backfill oder Cutover

Zwei Wege, **Operator-Entscheidung** (Michi):

- **Backfill** im selben Wartungsfenster, Fleet gestoppt: `UPDATE … SET <col> = <col> AT TIME ZONE 'Europe/Bucharest' AT TIME ZONE 'UTC'` auf den naiven Spalten. Danach ist die Historie einheitlich UTC und alle Kompensationen fliegen raus. Sauberster Endzustand, aber ein Schreibzugriff auf Money-Tabellen — Backup zwingend, die DST-Mehrdeutigkeit der Herbst-Stunden bleibt.
- **Cutover-Konstante:** ein R3-Zeitstempel in `core/time.py`; Leser lokalisieren Zeilen davor als Bukarest, danach als UTC. Kein Live-Write, dafür trägt jeder Trainer dauerhaft eine Verzweigung.

## 7. DDL-Wechsel auf `timestamptz`

Referenz-DDL: [`migrations/2026-07-r3-timestamptz.sql`](migrations/2026-07-r3-timestamptz.sql). **Nicht ausgeführt**, kein Runner.

Drei Bedingungen vor der Ausführung:

1. **Operator-Freigabe (C-Gate).** `ALTER TABLE` auf Live-Tabellen ist Eskalation.
2. **Der Flip aus §4 muss vorher liegen.** Sonst altert man Lokalzeit zu falschem UTC.
3. **Bootstrap-DDLs im selben PR mitziehen.** `CREATE TABLE IF NOT EXISTS` verbreitert nie eine bestehende Spalte — wer nur die Live-Tabelle altert, produziert genau die Repo-vs-Live-Drift, die uns P2.2 eingebracht hat (fünf Tage stumme Signale).

## 8. Rest-Backlog

- **P2.1** (`strategies/strat_fast_in_out.py`, `strat_5_percent.py`): Python-seitiger Vergleich naive Lokalzeit gegen UTC-`posted`. Von der Session-TZ **nicht** geheilt; `strategies/` ist ruff-excluded, DTZ greift dort nicht.
- **P2.3** (`3_detectors.py`), **P2.5** (`core/market_utils.update_cooldown`), **P2.6** (`5_trade_monitor.posted`): erledigt der Flip aus §4.
- **P2.4** (`closed_ai_signals.close_time`, drei Writer), **P2.21** (Cooldown/Outbox-Fenster in `28_signal_orchestrator.py`): mechanischer Nachzug auf `core/time.py`.
- **Der aware-Bypass.** `DTZ` flaggt nur *naive* Aufrufe. `datetime.now(timezone.utc)` bleibt erlaubt, und der Bestand hat davon ~79 Call-Sites in 34 Dateien (z.B. `26_regime_detector.py:216`, `core/signal_post.py`, `5_trade_monitor.py`). Die sind alle **korrekt** — nur eben nicht über `core/time.py` gezogen. `utc_now()` ist damit die *sanktionierte*, nicht die *einzige tatsächliche* Zeitquelle. Der Nachzug ist Fleissarbeit ohne Verhaltensänderung und gehört in denselben Folge-Task wie der Flip; ein Lint-Gate dafür gibt es nicht (ruff kennt keine Regel „nutze meinen Helper").
