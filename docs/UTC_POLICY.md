# UTC-Policy (R3)

**Stand:** 2026-08-01 · **Tasks:** T-2026-CU-9050-032 (Policy), T-2026-KYT-9050-005 (Flip) · **Root-Cause:** R3 (Audit) · **Cluster:** AUDIT_TODO P2.1–P2.6, P2.21

Kythera soll exakt eine Zeitdomäne haben: **UTC**. Diese Datei sagt, was davon jetzt gilt, was noch nicht gilt und in welcher Reihenfolge der Rest kommen muss.

---

## 1. Was jetzt gilt

| Ebene | Mechanismus | Datei | Status |
|---|---|---|---|
| Python | `utc_now()` / `utc_now_naive()` / `to_utc()` / `as_naive_utc()` / `from_unix_ts()` | `core/time.py` | **aktiv** |
| Lint | `ruff`-Regelgruppe `DTZ` verbietet naive `datetime.now()` / `utcnow()` / `fromtimestamp(ts)` | `pyproject.toml` | **aktiv** |
| Postgres | jede Pool-Session mit `-c timezone=UTC` | `core/database.py` | **im Repo (T-2026-KYT-9050-005)**, live ab dem nächsten Fleet-Restart |
| Historie | genau eine Konstante entscheidet die Lesart alter Zeilen: `core.time.R3_CUTOVER_UTC` | `core/time.py` | **Entscheidung offen, §6** |

Neuer Code kann keine naive Lokalzeit mehr einführen, ohne dass CI rot wird. Bestehende bewusste Ausnahmen tragen ein `# noqa: DTZ…` mit Begründung — das ist die sichtbare Rest-Schuld, kein Freibrief:

- `3_detectors.py` — **erledigt mit dem Flip** (P2.3): schreibt `utc_now_naive()` in `active_trades_master.time/posted`, das `noqa` ist raus.
- `30_ai_pex1_bot.py` — Watermark-Sentinel gegen `pump_dump_events.spike_time`. Achtung, die Begründung war falsch: die Spalte ist live `timestamp WITH time zone` (read-only vermessen 2026-08-01), nicht naiv. Siehe §3 und T-2026-KYT-9050-061.

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

Zielzustand ist überall `timestamptz`. **Ist-Spalte read-only gegen die Live-DB vermessen am 2026-08-01** (`information_schema.columns`) — die Domäne der naiven Spalten zusätzlich empirisch falsifiziert: Europe/Bucharest springt am 2026-03-29 um 03:00 vor, die lokale Wanduhr 03:00–03:59 **existiert an diesem Tag nicht**. Eine naive Spalte mit Zeilen in diesem Fenster kann keine Lokalzeit tragen.

| Tabelle | Spalten | Ist (live 2026-08-01) | Zeilen in der nicht-existenten Lokalstunde | Bootstrap-DDL |
|---|---|---|---|---|
| `active_trades_master` | `time`, `posted` | naiv | 0 (Referenzstunde ebenfalls 0 — Tabelle ist ein Rolling-Fenster) | `3_detectors.py` |
| `closed_trades_master` | `time`, `posted` | naiv | **0** bei 97/39 in der Referenzstunde → Lokalzeit bestätigt | `5_trade_monitor.py` |
| `closed_trades3` (SRA2-Retrain-Quelle) | `time`, `posted` | naiv, tot seit 2026-02-23 | — (Daten enden vor dem DST-Wechsel) | legacy |
| `trade_cooldowns` | `last_posted_at` | **live `timestamptz`**, Repo-DDLs uneinheitlich (P2.2) | — | `26`, `11`, `24`, `25` |
| `regime_history` | `ts` | naiv | **12** bei 12 in der Referenzstunde → **naiv-UTC bestätigt, keine Kompensation** | `26_regime_detector.py` |
| `regime_current` | `since`, `alt_context_since`, `last_raw_ts` | naiv (UTC-Writer) | — | `26_regime_detector.py` |
| `bot_regime_performance` | `last_computed` | naiv (UTC-Writer) | — | `26_regime_detector.py` |
| `bot_regime_whitelist` | `computed_at` | naiv (UTC-Writer) | — | `26_regime_detector.py` |
| `orchestrator_open_trades` | `opened_at`, `closed_at` | naiv (UTC-Writer) | 0 (Tabelle beginnt 2026-04-18) | `26_regime_detector.py` |
| `orchestrator_suppressed_signals` | `ts` | naiv (UTC-Writer) | 0 (dito) | `26_regime_detector.py` |
| `pump_dump_events` | `spike_time` | **live `timestamptz`** — die Repo-DDL (`10_pump_dump_detector.py:1409`) sagt `TIMESTAMP`, die Live-Tabelle wurde irgendwann gealtert. Genau daran stirbt Bot 30 (T-2026-KYT-9050-061) | — | `10_pump_dump_detector.py` |
| `ml_predictions_master` | `time`, `created_at` | naiv, **keine Repo-DDL** | 0 bei 170 in der Referenzstunde → Lokalzeit bestätigt | — (Lücke, R2/B3) |
| `master_ai_processed_signals` | `processed_at` | **live `timestamptz`** | — | `15_ai_master_bot.py` |
| `ai_signals` | `open_time` | **gemischte Domäne** — live verifiziert 2026-07-10 (T-044): Spalte ist `timestamp without time zone DEFAULT now()`, d. h. alle Writer, die `open_time` dem Default überlassen, stempeln Session-lokal (Bucharest). Ausnahme seit T-052: ROM1-Rows (`28_signal_orchestrator.insert_rom1_signal`) schreiben explizit naiv-UTC, damit der Lifecycle-Sync gegen das naiv-UTC `opened_at` matchen kann. Vereinheitlichung = R3-Flip (§4). Nach dem Flip stempelt der `DEFAULT now()`-Cast UTC — die Spalte wird eindomänig, ohne dass ein Writer angefasst werden muss. 2026-08-01: 3.196 Zeilen, davon 247 ROM1 | 0 (ROM1 beginnt erst 2026-05-27, der DST-Test greift hier nicht) | `28` (UTC), alle anderen AI-Bots (Default = lokal) |
| `closed_ai_signals` | `open_time`, `close_time` | **beide naiv** (live vermessen 2026-08-01) — die frühere Zeile „`close_time` bereits `timestamptz`" war **falsch**; `8_ai_trade_monitor.py:27` ist eine `CREATE TABLE IF NOT EXISTS`-DDL und hat die bestehende Spalte nie verbreitert (dieselbe Falle wie P2.2). Gemischte Writer = P2.4 | | `8_ai_trade_monitor.py:27` |
| `{sym}_{tf}`, `ticker_10s` | `open_time`, `ts` | bereits `timestamptz` | | — |

## 4. Der Flip — was er anfasst (T-2026-KYT-9050-005, im Repo)

`-c timezone=UTC` im Pool ist **kein Einzeiler**. Er verschiebt in einem Schlag die Domäne jeder naiven Spalte, die einen aware-UTC-Wert oder `NOW()` entgegennimmt, und musste deshalb zusammen mit allen abhängigen Stellen in EINEM Changeset landen. Bestandteile:

1. ✅ `core/database.py` — `_connect_options()` trägt `-c timezone=UTC` (`_DEFAULT_SESSION_TZ`).
2. ✅ `3_detectors.py` — `write_signal_atomic` stempelt `utc_now_naive()` in **beide** Spalten (`time` und `posted`; es war immer EIN `datetime.now()`-Aufruf für beide Werte, P2.3). **Pflicht**: ohne diesen Fix kippt der Flip `33_ai_fif1_bot.fifo_burst_counts` von korrekt auf Drift, während er `5_trade_monitor` (P2.6) und `core/market_utils.update_cooldown` (P2.5) repariert.
3. ✅ Die Kompensationen aus §5 entfernt — ersetzt durch EINE Konstante (§6).
4. ✅ Docstrings mitgezogen: Modul-Docstring und `to_utc_naive()` in `15_ai_master_bot.py`, `fetch_recent_signals()` und `fifo_burst_counts()` in `33_ai_fif1_bot.py`, die Header der vier Dataset-Builder und `tools/retrain_sra2.py`.
5. ⏳ Was mit der **Historie** passiert — Operator-Entscheidung, §6.

**Wirksam wird der Flip erst beim Fleet-Restart**, prozessweise: ein Bot zieht die neue Pool-Option beim Start. Bis dahin läuft die Fleet unverändert weiter.

Restart-Effekt: Zeilen von vor dem Restart tragen Lokalzeit und werden ab dann als UTC gelesen — sie erscheinen +2/+3 h in der Zukunft. Betroffen sind die kurzen Fenster (60 min Trade-Monitor, 1 h / 24 h FIF1-Burst-Dichte, 5 Tage AIM2-Signal-Stream); der Effekt läuft mit dem längsten Fenster aus. FIF1 postet daraus nichts: das Startup-Marking in `33_ai_fif1_bot.main()` hakt alles ab, was beim ersten Poll im Fenster liegt — und die scheinbar zukünftigen Zeilen liegen alle beim ersten Poll drin (das Fenster hat keine Obergrenze).

**Widerlegt (2026-08-01):** der frühere Satz „`30_ai_pex1_bot.detect_spike_time_offset_h` heilt sich nach dem Flip von selbst, kein Eingriff nötig" war falsch. Die Funktion subtrahiert ein naives `now` von `MAX(spike_time)`, und die Spalte ist live `timestamptz` — sie wirft seit mindestens 2026-07-19 in **jedem** Scan `can't subtract offset-naive and offset-aware datetimes`. Bot 30 hat in den vier jüngsten `logs/watchdog_debug_*` 8.166 Fehlschläge und keinen einzigen erfolgreichen Scan. Gefixt in T-2026-KYT-9050-061 (eigener Commit im selben PR).

## 5. Die Kompensationen — der eigentliche Grund für den Zuschnitt

Sechs Stellen rechneten die Drift explizit heraus. Sie waren **korrekt** und wären durch die Umstellung **falsch** geworden.

Präzise: die Pool-Option **allein** fasst sie nicht an — sie vergleichen naive Parameter gegen naive Spalten, und das ist session-unabhängig. Falsch werden sie in dem Moment, in dem die **Writer** UTC schreiben (P2.3 und der aware-Cast unter UTC-Session). Da Flip und Writer-Fix zwingend zusammen landen (§4.2), ist das dieselbe Umstellung.

| Stelle | Was sie tat | Jetzt |
|---|---|---|
| `15_ai_master_bot.to_utc_naive()` + `load_signal_stream.since_local` | AIM2-Signal-Stream: `ml_predictions_master.time` und `*_trades_master.time` von Bukarest nach UTC | delegiert an `core.time.legacy_naive_to_utc`; die SQL-Grenze an `utc_to_legacy_naive` (`since_bound`) |
| `tools/research_dataset_common.py` — `LOCAL_TZ` + `to_utc_naive()` | die geteilte Basis aller Research-Datasets | delegiert; `LOCAL_TZ` ist nur noch ein Re-Export von `core.time.LEGACY_WRITER_TZ` |
| `tools/aim2_build_dataset.to_utc_naive()` | AIM2-Trainings-Datensatz | delegiert an den geteilten Helfer |
| `tools/fif1_build_dataset.py` (importiert `to_utc_naive`) | FIF1-Trainings-Datensatz | erbt die Delegation |
| `tools/pex1_build_dataset.py` (importiert `LOCAL_TZ`) | PEX1-Trainings-Datensatz | **bleibt bewusst lokalisierend** — siehe unten |
| `tools/retrain_sra2.py` (lokalisiert `closed_trades3`-Zeiten) | SRA2-Retrain | delegiert; `closed_trades3` ist reine Vor-Flip-Historie |

**Die Ausnahme, und warum sie keine ist.** `pex1_build_dataset.spike_time_to_utc` lokalisiert weiterhin — aber nur, wenn `detect_offset_h` den Offset **an den Daten gemessen** hat (2/3 h). Das ist keine Annahme, die der Flip falsch macht, sondern eine Messung, die nach dem Flip 0 ergibt und den Zweig damit gar nicht mehr betritt; für die Live-Tabelle ist er ohnehin tot, weil `spike_time` `timestamptz` ist und der aware-Zweig davor greift. Ihn zu löschen hätte nur das Lesen alter Dumps kaputtgemacht. Die DST-Rezeptur liegt trotzdem zentral: der Aufruf ist `legacy_naive_to_utc(s, assume_legacy=True)` — der einzige sanktionierte `assume_legacy`-Aufruf im Repo.

Die Trainer sind der harte Teil: sie lesen **Historie**. Nach dem Flip enthält jede naive Spalte beide Domänen — Lokalzeit vor dem Restart, UTC danach. Weder „immer kompensieren" noch „nie kompensieren" ist dann richtig. Ein Trainer, der das ignoriert, produziert Train/Serve-Skew — genau den Fehlermodus, gegen den AIM2 gebaut wurde (P0.13). Deshalb hängt die Lesart jetzt an **einer** Konstante statt an sechs Kopien derselben Annahme.

## 6. Historie: Backfill oder Cutover — offene Operator-Entscheidung

Der Code ist so gebaut, dass **beide Wege offenstehen** und keiner davon noch einen Code-Change braucht. Es gibt genau einen Schalter:

```python
core.time.R3_CUTOVER_UTC   # None (Repo-Default) | Instant des Restarts
KYTHERA_R3_CUTOVER_UTC     # gleiche Semantik, pro Prozess, ISO-8601 UTC
```

- `None` ⇒ **uniform-utc**: jede naive Spalte trägt über ihre ganze Historie UTC. Das ist die Welt NACH einem Backfill.
- gesetzt ⇒ **cutover**: Zeilen, deren gespeicherte Wanduhr vor dem Instant liegt, werden als `Europe/Bucharest` gelesen, der Rest als UTC.

Jeder Leser der Fleet geht durch `legacy_naive_to_utc` / `utc_to_legacy_naive`; die Lesart wird beim Start geloggt (`R3-Zeitdomäne: …`), damit eine falsche Annahme nicht still bleibt.

### Was ein Backfill anfassen müsste (read-only vermessen, 2026-08-01)

| Tabelle | Spalten | Zeilen | Größe | Spanne |
|---|---|---|---|---|
| `ml_predictions_master` | `time`, `created_at` | 1.131.684 | 167,3 MiB | 2026-02-24 → jetzt |
| `closed_ai_signals` | `open_time`, `close_time` | 476.535 | 84,5 MiB | 2026-02-24 → jetzt |
| `closed_trades_master` | `time`, `posted` | 382.918 | 96,2 MiB | 2025-08-23 → jetzt |
| `closed_trades3` (SRA2-Retrain) | `time`, `posted` | 8.245 | 1,2 MiB | 2025-09-06 → 2026-02-23 |
| `ai_signals` | `open_time` | 3.196 | 70,2 MiB | 2026-02-25 → jetzt |
| `active_trades_master` | `time`, `posted` | 539 | 1,2 MiB | 2026-02-24 → jetzt |
| **Summe** | | **≈ 2,00 Mio Zeilen** | **≈ 420 MiB** | |

**Nicht anfassen** (empirisch bestätigt, §3): der ganze `regime_*`/`orchestrator_*`-Cluster trägt bereits naiv-UTC — `regime_history` hat 12 Zeilen in der lokal nicht existierenden Stunde. Ebenso raus: alles, was schon `timestamptz` ist (`pump_dump_events`, `trade_cooldowns`, `master_ai_processed_signals`, Kerzen, `ticker_10s`).

### Kosten und Risiken, Seite an Seite

| | **Backfill** | **Cutover-Konstante** |
|---|---|---|
| Live-Write auf Money-Tabellen | ja, ~2,00 Mio Zeilen | nein |
| Aufwand | ein Wartungsfenster, Fleet gestoppt, Backup zwingend | eine Zeile setzen |
| Laufzeit | **Schätzung, nicht gemessen**: die vier großen Tabellen lesen sich warm in ~4 s Gesamt-Seq-Scan; ein Voll-UPDATE schreibt neue Tupel + WAL + Index-Einträge in die 17 Btrees dieser Tabellen (8 davon stehen auf genau diesen Zeitspalten, HOT-Update entfällt damit). Größenordnung **Minuten** (grob 5–20), danach Autovacuum. Ein echter Schreib-Benchmark war aus dieser Session nicht zulässig (harte Regel 1). | 0 |
| Platzbedarf | bis zum Vacuum ~+420 MiB Bloat | 0 |
| Dauerhafte Komplexität | **null** — der Cutover bleibt `None`, `LEGACY_WRITER_TZ` wird toter Code | eine Konstante + eine Verzweigung in `core.time`. Der frühere Einwand „jeder Trainer trägt dauerhaft eine Verzweigung" gilt **nicht mehr**: die Verzweigung liegt einmal zentral, die Trainer sehen sie nicht. |
| Rest-Unschärfe | die ambige Herbststunde: **113 Werte** (54 `closed_trades_master.time`, 59 `.posted`, 1 `closed_trades3.time`) lassen sich nicht eindeutig zurückrechnen — ±1 h | dieselben 113 Werte (Series → NaT, d. h. der Trainer verwirft sie) **plus** ein ≤3-h-Band um den Cutover: Zeilen, die lokal in den letzten 2–3 h vor dem Restart geschrieben wurden, tragen eine Wanduhr jenseits des Cutovers und werden als UTC gelesen |
| Statistik über die Grenze | keine Grenze, keine Unstetigkeit | jeder Leser, der **nicht** durch `core.time` geht (Ad-hoc-SQL, Dashboards, die Studien aus §8), sieht am Restart-Tag einen 2–3-h-Sprung; Tages-Aggregate genau dieses Tages sind entsprechend verschoben |

### Was zusätzlich zu wissen ist

**Die Reihenfolge entscheidet über den Aufwand des Backfills.** Läuft er im selben Fenster wie der Restart und VOR dem Start der neuen Fleet, ist er unbedingt (`UPDATE … SET c = c AT TIME ZONE 'Europe/Bucharest' AT TIME ZONE 'UTC'`, alle Zeilen sind Legacy). Läuft er später, braucht er zwingend eine Untergrenze `WHERE c < '<Restart-Instant>'` — sonst konvertiert er die neuen UTC-Zeilen ein zweites Mal. **Empfehlung unabhängig von der Entscheidung: den Restart-Instant protokollieren.** Er ist die Voraussetzung für den späteren Backfill UND der Wert der Cutover-Konstante; ohne ihn ist nur noch die teure Variante übrig (Domäne pro Zeile raten).

**Bis die Entscheidung fällt, gilt `uniform-utc`.** Für die laufende Fleet ist das binnen Stunden bis Tagen richtig (die Fenster sind 1 h bis 5 d). Für einen **Retrain auf Vor-Flip-Historie** ist es falsch — dort steht ohne Cutover-Konstante die ganze Historie um 2–3 h verschoben. Konsequenz: **kein Retrain auf Legacy-Spalten, bis §6 entschieden ist**; die Builder loggen ihre Lesart in die erste Zeile ihrer Ausgabe, damit ein Lauf unter der falschen Annahme im Log sichtbar ist.

`ALTER TABLE`/DDL und der Backfill selbst sind **nicht** Teil dieses Changesets (Freigaberahmen T-2026-KYT-9050-005: ausdrücklich ausgenommen).

## 7. DDL-Wechsel auf `timestamptz`

Referenz-DDL: [`migrations/2026-07-r3-timestamptz.sql`](migrations/2026-07-r3-timestamptz.sql). **Nicht ausgeführt**, kein Runner.

Drei Bedingungen vor der Ausführung:

1. **Operator-Freigabe (C-Gate).** `ALTER TABLE` auf Live-Tabellen ist Eskalation.
2. **Der Flip aus §4 muss vorher liegen.** Sonst altert man Lokalzeit zu falschem UTC.
3. **Bootstrap-DDLs im selben PR mitziehen.** `CREATE TABLE IF NOT EXISTS` verbreitert nie eine bestehende Spalte — wer nur die Live-Tabelle altert, produziert genau die Repo-vs-Live-Drift, die uns P2.2 eingebracht hat (fünf Tage stumme Signale).

## 8. Rest-Backlog

- **P2.1** (`strategies/strat_fast_in_out.py`, `strat_5_percent.py`): Python-seitiger Vergleich naive Lokalzeit gegen UTC-`posted`. Von der Session-TZ **nicht** geheilt; `strategies/` ist ruff-excluded, DTZ greift dort nicht.
- **P2.3** (`3_detectors.py`), **P2.5** (`core/market_utils.update_cooldown`), **P2.6** (`5_trade_monitor.posted`): erledigt der Flip aus §4 — wirksam ab dem Restart.
- **P2.4** (`closed_ai_signals.open_time`/`close_time`, drei Writer), **P2.21** (Cooldown/Outbox-Fenster in `28_signal_orchestrator.py`): mechanischer Nachzug auf `core/time.py`. Der Flip macht `NOW()` in diesen Pfaden UTC, die gemischte **Historie** der Spalte bleibt aber (§6).
- **Leser ausserhalb der Fleet, bewusst nicht mitgezogen** (Analyse-Tools, kein Live- und kein Trainings-Pfad; sie lokalisieren Legacy-Spalten selbst und lesen damit Zeilen NACH dem Restart 2–3 h daneben). Nachzug = dieselbe Ein-Zeilen-Delegation an `core.time`:
  - `tools/funding_risk_study.py:130` `to_utc_aware()` (`closed_ai_signals.open_time`)
  - `tools/breadth_study.py:428` — **eigenständiger Bug, unabhängig vom Flip**: lokalisiert `regime_history.ts` als Bucharest, obwohl die Spalte naiv-**UTC** ist (Writer `26_regime_detector.py:216`; empirisch §3: 12 Zeilen in der lokal nicht existierenden Stunde). Der As-of-Join der BTC-Regime-Features steht damit **heute schon** 2–3 h daneben. Hier nicht mitgefixt — eine Korrektur ändert das Studien-Ergebnis und braucht einen Re-Run, also einen eigenen Task.
  - `tools/settlement_timing_study.py` (`closed_ai_signals.open_time`)
  - `tools/analytics_export.py` + `tools/dashboard/app.py` — tragen naive Werte **verbatim** durch und differenzieren nie über die Domänengrenze; ihre Header-Notiz „wall clock Europe/Bucharest" gilt ab dem Restart nur noch für die Alt-Zeilen.
- **Der aware-Bypass.** `DTZ` flaggt nur *naive* Aufrufe. `datetime.now(timezone.utc)` bleibt erlaubt, und der Bestand hat davon ~79 Call-Sites in 34 Dateien (z.B. `26_regime_detector.py:216`, `core/signal_post.py`, `5_trade_monitor.py`). Die sind alle **korrekt** — nur eben nicht über `core/time.py` gezogen. `utc_now()` ist damit die *sanktionierte*, nicht die *einzige tatsächliche* Zeitquelle. Der Nachzug ist Fleissarbeit ohne Verhaltensänderung und gehört in denselben Folge-Task wie der Flip; ein Lint-Gate dafür gibt es nicht (ruff kennt keine Regel „nutze meinen Helper").
