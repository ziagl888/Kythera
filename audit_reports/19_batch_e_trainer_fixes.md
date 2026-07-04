# 19 — Batch E: Trainer-Validitäts-Fixes, Walk-Forward-Simulator, Retrain-Kandidaten

**Stand:** 2026-07-04 · **Task:** T-2026-CU-9050-016 · **Branch:** `feat/t-2026-cu-9050-016` (nichts deployed, Fleet unangetastet)

Abdeckung: AUDIT_TODO P0.10, P0.11, P0.12, P0.13(-Vorbereitung), P1.29, P1.30, P1.31, P1.35.
Artefakte: ausschließlich `Documents\_X\staging_models\` (Replays unter `staging_models\replay\`).

---

## E1 — Trainer-Code-Fixes (reine Korrektheit, kein Training)

### P1.35 — `core/update_model.py` (zuerst gefixt, Voraussetzung für alles Weitere)
`replace(".model", "_v2.json")` war für `*_model.pkl`/`.joblib` ein No-op → `save_model()` überschrieb das **Original-Artefakt in-place**. Jetzt: `splitext`-Zielname + hartes Refuse, wenn Ziel == Quelle **oder** das Ziel bereits existiert.

### P0.12 — ABR1 pandas_ta-Spalten (Bot + Trainer)
- `18_ai_abr1_bot.py` und `Documents\_X\BT2-Datagrepper-for-ML.py` (versionierte Kopie: `trainers_x/BT2-Datagrepper-for-ML.py`): **Prefix-Matching** statt Exakt-Namen (`KAMA_9*`, `TSI_*`/`TSIs_*`, `BBL_*`, `DCL_*`, …) + **hartes ValueError** bei fehlender Quellspalte statt stillem `fillna(0)`.
- Bot: **Startup-Selbsttest** — Feature-Pipeline auf echten Daten von bis zu 3 Coins; konstantes kontinuierliches Feature → `exit(1)`. Genau der Fehlermodus, der das Modell monatelang unbemerkt auf 7/18 Features fahren ließ.
- Datagrepper: Konstanz-Assertion über den fertigen Trainingsdatensatz; max. 2 Worker mit BELOW_NORMAL statt `cpu_count()`.
- **⚠ Nebenfund (deploy-kritisch):** Der Ruff-Cleanup `052ba4c` hatte den **funktionslokalen `import pandas_ta`** aus b6735d9 als "unused" entfernt (der Import registriert den `df.ta`-Accessor — klassischer F401-Fehlgriff). Folge: Der Repo-Stand von ABR1 wäre nach einem Deploy auf **jedem Coin** mit `AttributeError` gestorben, vom per-Coin-`except` verschluckt — still toter Bot. Live (== b6735d9) war nicht betroffen. Import auf Modulebene wiederhergestellt. **Empfehlung:** Ruff F401 für Accessor-Bibliotheken (pandas_ta) via `# noqa` absichern; alle weiteren Bots auf dasselbe Muster prüfen.

### P1.29 — chronologischer Split + Threshold auf Validation (`qm_ml_trainer.py`, `smc_ml_trainer.py`)
- 70/15/15 entlang der Entry-Zeit (neu: `entry_time` wird in der Simulation erfasst) mit **Purge-Gap** (QM: `ORDER_EXPIRY`=50 Bars; SMC: 100 Bars — TD-Muster spannen ≤100, BB ≤60+40).
- Threshold-Scan nur noch auf dem **Validation**-Slice; das Test-Set bleibt unberührt und liefert die einzige ehrliche Zahl (wird separat ausgegeben und im pkl-`meta` gespeichert).
- Beide Trainer speichern nur noch nach `staging_models\` (nie mehr in-place über Produktions-pkls) und schreiben einen `meta`-Block (Split, Test-Stats, xgb-Version, n je Slice).

### P1.30 — QM-Fill-Logik (`qm_ml_trainer.py`, `qm_backtest.py`)
- SL-Durchstich einer Pending-Order ist keine "Invalidierung" mehr: Da der SL jenseits des Entrys liegt, hat dieselbe Kerze zwingend auch den Entry berührt → konservativ **fill-then-stop = sofortiger Verlust**. Vorher wurden genau diese garantierten Verlierer aus dem Datensatz **gelöscht**.
- **Kein TP-Win auf der Entry-Kerze** mehr (Reihenfolge intra-Kerze nicht feststellbar); TP-Bewertung beginnt mit der Folgekerze. `qm_backtest.py` bucht den fill-then-stop-Fall jetzt als regulären Verlust inkl. Fees/Drawdown.

### P1.31 — Trainer-Data-Loader
- `fetch_merged_data` (qm + smc): `try/finally conn.close()` (vorher leakte jeder Query-Fehler eine Pool-Connection), Skips als WARN geloggt.
- Harter `SystemExit` bei **<80% Coin-Abdeckung** in qm-, smc- und BT2-Trainer — vorher trainierte die Pipeline still auf 0–8 Coins und speicherte über das Produktions-pkl.

### P0.13-Vorbereitung — AIM1-Vokabular-Abgleich (nur Doku, kein Retrain)
Quelle live: `ml_predictions_master.model_name` (daraus baut `15_ai_master_bot.py` sowohl `ai_model` als auch `conv_source_bot`).

| | pkl-Dummies | Live-DB (distinct, gesamt) | Overlap |
|---|---|---|---|
| `ai_model_*` | 11: ATS1, EPD1, **MSI1**-{8,24,72,168}h_{pump,dump} (Typo!), nan | 16: EPD1, AIM1, ATS1, RUB1, BB_1H/4H, MIS1-8H/24H/72H/168H, ATB1, QM_1H/4H, TD_1H/4H, SRA1 | **2/16** (ATS1, EPD1) |
| `conv_bot_*` | 5: `5% Bot`, `Fast Bot`, `SR Bot`, `Volume Bot`, nan | 0 Conv-Namen in `ml_predictions_master` (Classic-Bots schreiben dort nicht mehr) | **0** |

- Alle 8 MIS-Dummies tragen die historische **MSI1**-Schreibweise → live schreibt `MIS1-72H` etc. → One-Hot immer 0.
- Booster-Gains: `conv_bot_nan` ist mit **48,3** die stärkste Identity-Spalte (das Modell hat "kein Conv-Bot" als Feature gelernt — live ist das IMMER an); `ai_model_MSI1-72h_pump` 38,8, `ai_model_ATS1` 33,1 — alle live tot.
- Zusätzlich in `closed_ai_signals`: die Namenslandschaft wechselte am 2026-03-02 (MIS1-*_pump/dump und MSI1-* enden dort; MIS1-xxH beginnen) — jedes Identity-Vokabular ohne Versionierung veraltet binnen Wochen.
- **Konsequenz (mit Report 13/16 deckungsgleich):** Retrain nur aufs Vokabular reicht nicht (Volatilitäts-Label + round-Join bleiben) → AIM1 bleibt **Abschalt-/Neuprojekt-Empfehlung**, kein Batch-E-Retrain. Detaildaten: `p013_result.json` (Job-tmp) bzw. Tabellen oben.

---

## E2 — Walk-Forward-Simulator (`tools/walkforward_sim.py`)

Ein gemeinsamer Simulator statt 8 Ad-hoc-Backtests (X-R1-Fix, == P0.10):

- **Setup-Funktionen der Bots selbst**: UFI1 via Import von `find_ufi1_setup` (29), ABR1 via Import von Feature-Builder + `find_pivot_levels` (18), TD/BB als 1:1-Nachbau der Erkennung aus `25_smc_ml_sniper.scan_market` (inkl. aller FIX-Gates: MAX_TD_SPAN, MAX_BB_AGE, Frische-Bedingungen).
- **Geometrie = gepostete Geometrie**: `calculate_smart_targets` hat jetzt einen optionalen `df`-Parameter — dieselbe Live-Funktion läuft im Replay auf dem historischen 1000-Kerzen-Fenster bis zur Entscheidungskerze (kein Copy-Paste-Skew, inkl. des Live-Fallback-Verhaltens).
- **Nur geschlossene Kerzen**; Entscheidung je geschlossener Kerze; Cooldowns/Active-Trade-Dedup wie die Bots.
- **Exits**: wick-aware First-Touch-Forward-Scan über 1h-Kerzen, **SL-first bei Ambiguität**, Trailing wie `8_ai_trade_monitor` (ab TP2 → SL auf `targets[k-2]`), Positions-Fraktionierung über die publizierten TPs (UFI1: 1, ABR1: 3, TD/BB: 5), **Fees 0,05%/Seite** (P3.6).
- Betriebsschutz: BELOW_NORMAL-Priorität (wintypes-korrektes ctypes-Fallback), CPU-Check >90% → Abbruch, DB-Session read-only, Output JSONL nach `staging_models\replay\`.

**Bewusste Näherungen (dokumentiert):** UFI1-Scan je Daily-Close statt alle 4h; TD/BB-Scan je geschlossener Kerze statt alle 3 min; ABR1-Indikatoren einmal über die Gesamtserie statt je 240h-Fenster (== Trainer-Verhalten; rekursive Indikatoren konvergieren); Funding-Kosten nicht modelliert; DB-Indikatoren historisch mit R1-Restrisiko (Forming-Candle-Überschreibungen).

### P0.11-Validierung: UFI1 "+278R" fällt

Full-Universe-Replay (648 Coins, 365 Tage, Juli 2025 – Juli 2026), exakt die Live-Geometrie (CMP-Entry, Single-TP1, SL = Swing-High +3%):

| Metrik | Backtest-Claim (`fib_backtest.py`) | Ehrlicher Walk-Forward |
|---|---|---|
| Trades | 334 | 435 (384 geschlossen, 51 offen) |
| WR (TP1 first-touch) | 54,2% | 50,8% |
| Ø R | **+0,83R** | **+0,37R** |
| Σ R | **+278R** | **+141R** |

Und die +141R zerfallen bei Kohorten-Betrachtung:

| Kohorte | n geschlossen | WR | Σ R |
|---|---|---|---|
| **2025-10 (Crash-Monat)** | 216 | **78,2%** | **+184,7R** |
| alle übrigen 11 Monate | 168 | **~14%** | **−44R** |
| davon 2026-06 (Live-Ära) | 16 | 37,5% | +1,8R |

1. **Der gesamte Ertrag ist ein Ein-Monats-Artefakt** (Oktober-2025-Crash: 60%-Dumps überall, Shorts in den Bärenmarkt hinein mit 4-Monats-Haltezeiten). Ohne diesen Monat ist die Strategie klar negativ.
2. **Simulator-Realitätsabgleich bestanden:** Juni-2026-Kohorte 37,5% WR (n=16) vs. live 25,7% (n=35) — konsistent; live zusätzlich belastet durch Forming-Daily-Candle-Repaint (R1: der Bot liest die laufende Tageskerze) und Monitor-Fehlscoring (Report 17).
3. **Hebel-Realität (der eigentliche Todesstoß):** Max-Adverse-Excursion über die geschlossenen Trades — **72% aller Trades (und 72% der Gewinner) laufen ≥+5% ins Minus** (Median MAE 9,6%, p90 41,9%). Bei den ursprünglich geposteten 20x (Liquidation ~+5%) wäre die Mehrheit der Replay-"Gewinner" **vor dem TP liquidiert** worden. Selbst der Papier-R-Wert ist also nur mit ≤1-2x Hebel (nach P0.6-Fix) überhaupt realisierbar — und dann bleibt ex-Oktober ein Verlustgeschäft.

**Deploy-Empfehlung UFI1: AUS lassen** (bestätigt Report-16-Note F). Kein Retrain — es gibt keine Selektionsschicht, die den strukturellen Befund heilen würde.

---

## E3 — Retrains auf Replay-Labels (Staging)

Kandidatenwahl nach Report 16 + E2: **TD_1H/4H** (beste Kalibrierung, netto positiv), **BB_4H** (+BB_1H-Daten zur Prüfung), **ABR1** (nach P0.12 erstmals mit 18/18 Features). NICHT retrainiert: **AIM1** (Abschalt-Empfehlung, s.o.), **UFI1** (Abschalt-Empfehlung), **QM** (Report 16: QM_4H stoppen, QM_1H parken — Exit-Geometrie gibt alles zurück, das löst kein Retrain), **MIS1** (Retrain-Priorität #1 laut Report 16, braucht aber den 67-Feature-Builder + Horizont-Labels — eigener Task, siehe "Nicht gemacht").

Methodik je Modell (`tools/retrain_from_replay.py`): Label = First-Touch-TP1-vor-SL der **geposteten** smart-targets-Geometrie (Fees inkl.); chronologischer 70/15/15-Split mit Purge-Gap; Threshold per **realem Replay-PnL** auf Validation; Isotonic-Kalibrierung (als Zusatz-Key im Artefakt); Kalibrierungs-Report alt vs. neu auf identischen Test-Events.

<!-- RETRAIN_RESULTS -->

---

## Nebenfund zusätzlich: BB_1H-Parking deckt nur die LONG-Seite

`25_smc_ml_sniper.py:254` gated `tf != '1h'` nur im Breaker-Block-**LONG**-Zweig; der SHORT-Zweig (`:283`) hat kein TF-Gate → **BB_1H SHORT feuert live weiter**, obwohl Report 14/16 BB_1H als geparkt führen (−1.089 netto). Fix ist eine Zeile — Empfehlung: SHORT-Zweig gleichziehen oder Parking bewusst dokumentieren.

## Nicht gemacht (und warum)

- **MIS1-Retrain** (Report-16-Priorität #1): braucht den 67-Feature-Builder aus `X5-analyze_indicators_v8.py` inkl. Entfernung der Leakage-`line_cols`, Horizont-Label über den First-Touch-Simulator und die R1-geschlossene-Kerzen-Disziplin — ein eigener, größerer Task; Batch E liefert dafür jetzt Simulator + Gerüst.
- **AIM1/UFI1-Retrain**: bewusst nicht — beide Abschalt-Empfehlungen (Begründung oben bzw. Report 16).
- **QM-Retrain**: Trainer ist jetzt korrekt (P1.29/P1.30), aber Report 16 zeigt, dass QMs Problem die Exit-Geometrie ist, nicht die Selektion; erst Exit-Redesign, dann Retrain.
- **R1 (Forming Candle) / Monitor-Rewrite-Rest**: nicht Teil von Batch E; die Replay-Labels umgehen beide Probleme (eigene Exits aus Kerzen, keine Monitor-Labels).
- **Funding-Kosten im Simulator**: nicht modelliert (mehrmonatige UFI1-Holds wären in Bärenphasen sogar leicht begünstigt — Shorts erhalten meist Funding); für TD/BB/ABR1 (Stunden bis Tage Haltezeit) untergeordnet.
