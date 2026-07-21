# OPUS-HANDOFF — Operating Manual Kythera

**Stand:** 2026-07-09 (Ledger-Verifikation T-2026-CU-9050-028) · **Basis:** 2026-07-07, T-2026-CU-9050-021 (letzter Fable-5-Tag) · **Schwester-Handoffs:** T-2026-CU-9000-296 (claude_skills), T-2026-CU-9000-297 (knowledge_scraper)

Ab 2026-07-08 übernimmt Opus die Task-Abarbeitung in diesem Repo. Dieses Dokument ist das Operating Manual: Arbeitszyklus, System-Synthese, kuratierte Fallen, Qualitätsbar, Eskalationsregeln. Es ergänzt `CLAUDE.md` (harte Regeln, auto-geladen) und `docs/T-2026-CU-9050-021-opus-task-audit.md` (geranktes Backlog). **Am Session-Start lesen, vor dem ersten Edit.**

---

## §1 Kontext in drei Sätzen

Kythera ist Michis produktiver Crypto-Trading-Bot (~29 Bots, ~530 Coins, Binance Futures, Windows-VPS, PostgreSQL). 2026-06/07 lief ein vollständiger Audit (20 Reports, 126 Findings in `AUDIT_TODO.md`); die Money-kritischen Code-Bugs sind größtenteils gefixt, offen sind strukturelle Root-Causes (R1–R4), das Retrain-Programm und der DB/Perf-Block (Z0, TimescaleDB). Es gibt **zwei Maschinen**: die Build-Maschine (dieses Checkout, keine DB-Credentials) und den Live-VPS (Bots + DB + echtes Geld + Trainer in `Documents\_X`).

## §2 Kanonischer Arbeitszyklus

0. **`git fetch origin` + Stand prüfen** — vor jeder Priorisierung, vor dem ersten Edit (Falle 15).
1. **Task wählen** — Backlog-Reihenfolge aus dem Task-Audit-Doc; pro Task zuerst `read_doc` des KB-Task-Docs (die Briefs dort sind eine Interpretation vom Stand-Datum, die KB kann weitergedreht sein).
2. **KB-Task starten** (`/task-start T-2026-KYT-9050-NNN` — Nummernkreis seit 2026-07-21, siehe CLAUDE.md §Workflow; der alte `T-2026-CU-9050-NNN`-Kreis ist geschlossen, neue Tasks via `add_task` mit `customer/project_id="kythera"`), Worktree, Branch `feat/<t-id>`.
3. **Vor Lösungsideen: Problem in 4 Fragen zerlegen** (Skill `z-fable-judgment`): Outcome / Population / Messung / Stop-Kriterium. Wenn eine nicht beantwortbar ist → Rückfrage, nicht Annahme.
4. **KB-first:** `search_kb` nach Präzedenzfall (Decisions, Kythera-Tasks im KYT- **und** dem geschlossenen CU-9050-Korpus, Audit-Reports). Fast alles hier hat einen dokumentierten Vorentscheid.
5. **Implementieren** — Konventionen aus §4/§5. Bei Bot-Edits vorher die DO-NOT/WARNING/forming/lookahead-Kommentare in der Datei greppen (~69 Stück über 40 Dateien — sie markieren die Minenfelder).
6. **Verifizieren** (§7) — CI reicht nicht.
7. **PR** (Englisch, conventional commits — ziagl888-Repo, kein Org-Titel-Format), Kern-Reviews (z-code-reviewer + z-spec-compliance-review). **Merge via merge-train (Default seit 2026-07-10):** nach PASS `cu/reviews` auf den Head-SHA stempeln, `gh pr edit <PR#> --add-label merge-train`, Session schließen — der Daemon (`services/merge_train/` in knowledge_base_internal, Hetzner) merged seriell und rebased jeden PR höchstens einmal. **NICHT selbst `gh pr merge`** — parallele Fleet-Sessions erzeugen sonst die O(n²)-Rebase-Kaskade über die CHANGELOG-Top-Insertion (die Konflikt-Mühle vom 2026-07-10). Bounce = Label `merge-train:failed` + Daemon-Kommentar; Re-Queue braucht neuen Commit + Re-Stamp + Re-Label (Label-Re-Add allein ist bewusster No-op, und ein Rebase/Force-Push verwirft den `cu/reviews`-Status mit). Nach dem Enqueue nicht auf den Merge idle-pollen — CHANGELOG-/AUDIT_TODO-Nachlauf gehört in den PR selbst.
8. **Nachlauf:** `CHANGELOG.md`-Eintrag (Deutsch, Stil wie bestehende Einträge), `AUDIT_TODO.md`-Checkboxen der erledigten Findings flippen (inkl. ✅-Datum), KB-Task-Status, ggf. Folge-Task anlegen.

**No-op ist ein gültiges Done.** "Finding widerlegt", "kann der Bestand schon", "Effekt bleibt aus (Stop-B)" sind erfolgreiche Ergebnisse — dokumentieren statt Pseudo-Output bauen.

## §3 System-Synthese (Was wo lebt)

- **Datenfluss:** Binance WS (`wss://fstream.binance.com/market/…` — Legacy-Endpoints sind seit 2026-04-23 tot) → `1_data_ingestion` → per-Coin-Tabellen (~9.297, `{sym}_{tf}`) → `2_indicator_engine` (~120 Indikatoren) → Strategy-/AI-Bots → `28_signal_orchestrator` (Regime-Whitelist, Dedupe, EIN Cornix-Channel) → `telegram_outbox` → `4_telegram_bot` → Cornix → Binance. Monitore 5/8 scoren SL/TP.
- **Prozess-Lifecycle:** `main_watchdog.py` ist der einzige Owner (seit 8d3145f). Parken: Marker `control/parked/<script>.py`; One-Shot-Restart: `control/restart/<script>.py`. Health-Checks alle 60s (`core/health_monitor.py`).
- **Regime-Schicht:** Bots 26 (Detector, 5 BTC-Klassen × 3 Alt-Kontexte) / 27 (per-Bot-Performance → Whitelist) / 28 (Gating). Doku: `docs/REGIME_ORCHESTRATOR.md` (live, aber Spec-Drift P1.10 beachten).
- **ML-Programm:** Seit dem Audit gilt: **Labels nur aus Walk-Forward-Replay der echten Order-Geometrie** (`tools/walkforward_sim.py`, first-touch TP1-vs-SL, Fees) — nie Close-basierte Proxys. Retrain-Pipeline: `tools/retrain_from_replay.py`, AIM2: `tools/aim2_build_dataset.py` + `aim2_train.py`. Modell-Intents: `docs/MODEL_INTENT.md`. AIM2-Design: `docs/AIM2_DESIGN.md` (Bot 15, shadow-first via `AIM2_LIVE_POSTING`). Research-Bots 30–33 (PEX1/FMR1/TRM1/FIF1): `docs/NEW_IDEAS_BOTS.md`, gated via `NEW_IDEAS_LIVE_POSTING`, ohne Artefakt laufen sie im Idle-Mode.
- **Geparkt per Audit-Entscheid:** `14_ai_atb_bot.py`, `29_ufi1_bot.py`. **AIM1 ist tot** (invertierte Kalibration, P0.13 — Entscheid: KEIN Retrain, bleibt aus; AIM2 ist der Ersatz).
- **Staged-C-Refactor** (T-2026-CU-9050-007, Premortem in `.local/refactor/` — gitignored): Strangler-Fig innerhalb Kytheras, TimescaleDB-Fundament neben dem Bestand, Strategien einzeln hinter Paritäts-Gates. Phase 0 done, Phase 1 (Regression-Guard) gebaut **und scharf** — 24 Goldens + 24 Fixtures + Manifest sind seit `4765e25` git-tracked, `guard.py verify` läuft als pre-commit-Hook (Korrektur 2026-07-09: dieser Absatz und das Task-Audit sagten fälschlich „nicht scharf"; offen ist nur die Disarm-Härtung P2.51). Der alte v4-Repo ist tot — nicht wiederbeleben. Leitsatz aus dem Premortem: **"Green means like-v2, never correct."**

## §4 Die kuratierten Fallen (was schwächere Modelle hier falsch machen)

1. **Forming Candle (R1).** `is_closed` ist noch NICHT in der DB durchgesetzt (Design: `docs/TIMESCALE_R1_MIGRATION.md`). Bots schützen sich individuell — teils `iloc[-1]` auf DESC-sortierten Frames (neueste Zeile = Index 0!), teils Drop der letzten Zeile. Wer Indexierung "aufräumt", ohne die Sortierung zu prüfen, baut Look-ahead ein.
2. **Geteilte Feature-Builder (X-R1-Regel).** `core/{mis,aim2,rub,funding,research}_features.py` werden von Bot UND Trainer/Replay importiert. Ein "harmloser" Refactor dort verschiebt die Feature-Verteilung eines Live-Modells. Feature-Contract ist hart: fehlende Spalten führen zum Load-Fehler/Idle, nicht zu `fillna(0)` (die P0.12-Lektion).
3. **Idle-Mode ≠ kaputt.** Bot ohne deploytes Artefakt startet und tut nichts (`loaded=False`). Nicht "fixen".
4. **Staging-Regel.** Trainings-Tools schreiben nur nach `staging_models/`. Ein Trainer, der auf den Live-Artefakt-Pfad zeigt, ist ein Bug — auch wenn es "praktischer" wirkt.
5. **Cornix-Doppel-Parse.** Eine zweite Message mit identischem Signal-Block = doppelte Position mit echtem Geld. Info-Message ohne Cornix-Block, immer.
6. **Per-Coin-Tabellen.** Es gibt (noch) keine `candles`-Tabelle. Tabellennamen sind f-Strings aus `coins.json` — Identifier-Hygiene beachten (P3.3), Symbol-Whitelist nicht aufweichen.
7. **coins.json hat zwei Writer** (`1_data_ingestion.update_trading_pairs` + `6_housekeeping.update_coins_json`) — Filter müssen identisch bleiben (quoteAsset=USDT + PERPETUAL), sonst lecken Junk-Symbole fleet-weit (der "ETHU"-Vorfall).
8. **Caller-Commit-Kontrakt.** `core/signal_post.py`/Cooldown-Helper committen nicht. Wer den Caller-Commit vergisst, persistiert nichts — oder partiell.
9. **TZ-Minenfeld (R3 offen).** Writer schreiben UTC, diverse Reader lesen naiv-lokal. Vor jedem Fix an Zeitfenstern/Cooldowns/Stats das AUDIT_TODO-TZ-Cluster (P2.1–P2.6) lesen — Einzel-Fixes ohne die R3-Linie (core/time.py, timestamptz) erzeugen neue Drift.
10. **Windows-Realität.** Live-VPS ist Windows: `platform=win32` (mypy), win32-Prozess-Prioritäten, SIGBREAK, PG-Datadir `C:\PGDATA`, Backups `D:\_BACKUP\db`, PowerShell 5.1 (keine `&&`-Chains). `terminate()` ist ein Hard-Kill (P2.48).
11. **CI-Lücke.** CI = ruff/format + mypy + AST/Import-Smoke + Secret-Regex. Kein pytest, kein Guard, keine Backtests. Verhaltens-Verifikation ist Bringschuld der Session (§7).
12. **Ruff/mypy-Excludes.** `backtest/`, `tools/`, `strategies/`, `handlers/`, `trainers_x/`, `legacy_trainers/` sind excluded — dort ist der Lint-Bar bewusst niedriger. Nicht "aufräumen" als Selbstzweck (Boy-Scout nur mit Touch-Kontext).
13. **AUDIT_TODO-Annotation ≠ Wahrheit.** Bis 2026-07-09 stand hier „erst Annotation lesen, dann handeln". Das reicht nicht: bei der Verifikation (T-028) stellte sich **eine der Annotationen selbst als falsch heraus** — P1.26 war als widerlegt markiert, ist aber ein realer Dead-Code-Bug; der „Beweis" waren Cooldown-Rows einer älteren Codeversion, deren Key der aktuelle Code gar nicht mehr schreibt. Regel also: **erst Annotation lesen, dann am Code nachprüfen, dann handeln.** Ein Live-Zähl-Beweis („N Rows, also feuert der Pfad") ist nur gültig, wenn der aktuelle Code diesen Key auch tatsächlich schreibt.
14. **MIS2-SHORT Limit-Entries** werden vom Trade-Monitor noch falsch gescored (unfilled +5%-Entries dürfen nicht zählen) — bekannter Follow-up, nicht als neuen Bug melden.
15. **Der Checkout kann hinter `origin` liegen.** Mehrere Sessions arbeiten parallel am selben Repo; am 2026-07-09 war der Build-Checkout 8 Commits zurück. Wer aus einem stale Checkout priorisiert, sieht die Fixes nicht, die schon liegen, und arbeitet sie erneut ab. **Vor jeder Priorisierung `git fetch origin` + Stand prüfen** — vor dem ersten Edit, nicht danach.
16. **Modell-Tag kommt aus dem Artefakt, nie aus einer Konstante.** Harte Regel 6 wird nicht dadurch erfüllt, dass die Quellcode-Konstante zufällig zur aktuellen Generation passt. Drei Bots (`11_ai_mis`, `13_ai_rub`, `24_quasimodo`) werfen die geladene `meta.model_id` weg (P1.45) — beim nächsten Retrain verschmelzen die Generationen still in der Per-Bot-Statistik, auf der das Orchestrator-Gating entscheidet. Korrektmuster: `18_ai_abr1_bot.py:520`. **Vor jedem Retrain-Rollout prüfen, ob der Post-Pfad die `model_id` wirklich liest.**

## §5 Qualitätsbar pro Deliverable

- **Code-Fix (Bot/Core):** Root-Cause benannt (kein Symptom-Patch); betroffene DO-NOT-Kommentare respektiert; `backtest/test_*.py` der berührten Fläche grün; ruff+mypy lokal grün; CHANGELOG-Eintrag; AUDIT_TODO-Checkbox geflippt. Bei Geld-Pfad (Signal-Emission, Monitor-Scoring, Orchestrator-Gating): zusätzlich der Beweis im PR-Text, dass sich die Live-Semantik nur wie beabsichtigt ändert.
- **Neuer Bot/Strategie:** teilt Feature-/Detection-Source mit Trainer+Replay (ein Modul in `core/`); postet nach `CH_NEW_IDEAS` hinter default-off-Gate; Artefakt-Loading via `core/model_artifacts.py` (Idle-Mode-fähig); eigene `docs/MODEL_INTENT.md`-Sektion; Cooldown + Dedupe von Tag 1.
- **Trainer/ML:** Labels aus Walk-Forward-Replay; chronologischer Split + Purge-Gap; Kalibrierung + Threshold auf Validation-Slice; Artefakt nach `staging_models/` mit Meta (`model_id`, Feature-Namen, Versionen); Kalibrierungs-Report; **kein Rollout** — Rollout-Empfehlung an Michi.
- **Doku:** Deutsch für Betriebs-/Audit-Doku (Stil AUDIT_TODO/CHANGELOG), Englisch für Code-nahe Doku; Intent rekonstruierbar für den Folge-Agenten; kürzer als der Code, den sie beschreibt.

## §6 Eskalationsregeln

**Sofort stoppen und Michi fragen** (irreversibel · Geld · Außenwirkung · Gate-Flip):

- Artifact-Promotion aus `staging_models/` in den Live-Pfad; jeder Retrain-**Rollout** (Training selbst + Staging-Kandidat + Empfehlung sind ok).
- Gate-Flips: `AIM2_LIVE_POSTING`, `NEW_IDEAS_LIVE_POSTING`, Orchestrator-Gating-Parameter, Parken/Entparken.
- Fleet-Restarts, `.env`-Änderungen, alles was laufende Prozesse auf dem VPS berührt.
- Schema-Änderungen/Migrationen an Live-Tabellen (insb. T-2026-KYT-9050-002, ex-CU-9050-018 — dort sind Operator-Entscheidungen explizit offen).
- Löschen von Daten/Tabellen (auch "tote" — D5 nur nach Freigabe).
- Zwei gleichwertige Wege mit strategischer Konsequenz (z.B. Scope-Erweiterung Staged-C).

**Nicht eskalieren, einfach machen:** reversible Code-Fixes im Worktree, Tests, Doku, Ledger-Hygiene, Analysen/Studien ohne Live-Eingriff, Staging-Trainings auf dem VPS innerhalb der Batch-E-Constraints (CPU-gedrosselt, Live-Tabellen read-only, keine Produktions-pkls).

**3-Versuche-Regel:** Drei Fix-Versuche ohne neue Diagnose-Information → stoppen, Hypothesenraum neu aufspannen, ggf. eskalieren. Raten skaliert nicht.

## §7 Verifikations-Matrix

| Änderung an … | Pflicht-Verifikation |
|---|---|
| `2_indicator_engine.py` / Indikator-Pfad | `python tools/regression_guard/guard.py verify` (wenn armed; sonst `smoke`) |
| `core/*_features.py` | zugehörige `backtest/test_*_features.py` + betroffener Trainer lädt Artefakt noch (Feature-Contract) |
| Bot-Signal-Logik | Standalone-Test der Datei (AST/Import), betroffene `backtest/test_*.py`, Grep auf Cornix-Block-Dopplung |
| Orchestrator/Regime (26/27/28) | `backtest/test_signal_orchestrator.py`, `test_regime_detector.py`, `test_bot_regime_analyzer.py` |
| Monitore 5/8 | Scoring-Semantik gegen `audit_reports/17_monitor_replay_and_gaps.md` prüfen (63.4%-Agreement-Vorgeschichte) |
| Trainer/Replay | Mini-Lauf auf kleinem Coin-Set gegen `staging_models/`, Kalibrierungs-Report |
| Alles | ruff + `ruff format --check` + mypy lokal (= CI), pre-commit durchlaufen lassen (nie `--no-verify`) |

Auf der Build-Maschine ist die DB nicht erreichbar — DB-abhängige Verifikation (Guard `extract`/`verify` armed, Live-Queries, Trainings) gehört in eine VPS-Session.

## §8 How Fable Thinks

Die Denkmuster liegen im Skill **`z-fable-judgment`** (Problem-Zerlegung in 4 Fragen, billigste Falsifikation zuerst, Empfehlung statt Survey, Default-off für Unbewiesenes, No-op/Stop-B-Disziplin, 3-Versuche-Regel). Kythera-eigene Kalibrier-Beispiele:

- **AIM1 (P0.13):** Naheliegend wäre "Vokabular retrainen". Entscheid: NEIN — der Retrain hätte das invertierte Volatilitäts-Modell reproduziert. Muster: Root-Cause vor Mechanik-Fix; ein totes Modell aus lassen ist ein gültiges Done. AIM2 wurde stattdessen sauber neu gebaut (Replay-Labels, shadow-first).
- **Walk-Forward-Regel (P0.10):** 7/8 Trainer-Familien labelten idealisierte Fills, die die Bots nie handeln. Entscheid: EIN gemeinsamer Simulator statt acht Einzel-Fixes. Muster: Root-Cause-Werkzeug vor Punkt-Fixes.
- **Staged-C statt v4-Rewrite:** Der Big-Bang-Rewrite war schon einmal gestorben (40 Commits in 3 Tagen → 3 Monate Stille). Entscheid: Strangler-Fig im Bestand, Guard zuerst, WIP=1. Muster: Momentum-Risiko schlägt Architektur-Eleganz.
- **Batch-E-Disziplin:** Jede Strategie-Idee wird billig falsifizierbar gemacht (Replay entscheidet, ~1 Tag), bevor Live-Code entsteht — siehe T-2026-KYT-9050-003 (ex-CU-9050-020, HMM-Studie) als Vorlage.
