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
