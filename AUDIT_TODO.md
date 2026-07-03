# Kythera — Audit-Todoliste (Step 1: Code-only)

**Stand:** 2026-07-03 · **Methode:** 12 parallele Read-only-Reviews über das gesamte Repo (Core/Watchdog, Datenpipeline, Telegram/Monitore, Orchestrator/Regime, klassische Strategien, AI-Bots ×3 Gruppen, Market-Intelligence, Dashboard/Tools, ML-Trainer/Backtests, Cross-Cutting-Sweep).

**Was das ist:** Findings **rein auf Code-Basis**. Wo ein Punkt erst mit Live-Daten endgültig beurteilbar ist, steht `[DB]` — diese Punkte gehen in **Step 2** (VPS + Datenbankzugriff) zur Bestätigung/Messung. Der Step-2-Query-Katalog steht ganz unten.

**Legende:** `[ ]` offen · Schweregrad **P0** (geld-kritisch, sofort) → **P3** (Kosmetik). `[DB]` = in Step 2 verifizieren. Datei:Zeile ist anklickbar.

**Step 3 (2026-07-03) ist gelaufen** — ML-Trainer-Audit über `Documents\_X` (6 parallele Reviews, Trainer ↔ Live-Bot ↔ Artefakt-Introspektion): `audit_reports/13_x_ml_trainers.md`. Kurzfassung: ABR1-11/18-Features-Bug im Trainer bewiesen (Split-Counts=0), AIM1-Inversion code-erklärt (Volatilitäts-Label + round-Join-Lookahead + totes Vokabular), RUB1 mit stillem MACD-9/21↔12/26-Bruch, ATB1 trainiert auf anderem Event-Typ als es handelt, EPD1 wird live ohne das Trainings-Gate befragt, MIS1 ohne jede Provenienz + Ticker-Leakage; P1.18 widerlegt; SRA1-v2 = bewiesene Formatkonvertierung von v1.

**Step 6 (2026-07-03): Strategie-/Modell-Konzeptbewertung** — alle 26 Strategien/Modelle konzeptionell bewertet (Edge-Hypothese, Trainingsvalidität, Live-Evidenz aus Report 14) mit Note A–F, Gesamtranking und Portfolio-Empfehlungen: `audit_reports/16_strategy_concept_evaluation.md`. Kurzfassung: Top-Kandidaten MIS1-72H, TD, SRA1, Support Resistance (je B−); verlässlich schädlich AIM1, UFI1, Fast In And Out, QM_4H (je F); Meta-Ebene C+ (ROM1-Mehrwert real, aber die 4D-Regime-Hypothese ist ungetestet — TRANSITION-Restklasse deaktiviert das Gate 44,5% der Zeit); kein Modell hat aktuell belegbaren ML-Skill — die Gewinne stammen aus S/R-Trade-Konstruktion + Regel-Gates + Marktregime.

**Step 6 (2026-07-03): Regime-Orchestrator-Gesamtanalyse** — `audit_reports/16_regime_orchestrator_analysis.md`. Kurzfassung: ROM1 liefert +8pp WR-Mehrwert, aber die Whitelist ist zu 89% Default-Open (747× insufficient_data), die 4D-Matrix hat median 7 Trades/Zelle, der Detector hat in 5,5 Monaten nie ein TREND-Regime gehalten (7 Episoden, alle <1h) und flappt in 52% der Episoden; Auto-Close kappt 49% der Trades im Gewinn (median 0%). Vorschläge in 4 Stufen inkl. Suppressed-Counterfactual-Scorer und Empirical-Bayes-Shrinkage statt 4D-Whitelist.

**Step 5 (2026-07-03): Strategie-Vorschlagsliste** — datengetriebene Konzepte für neue Long/Short-Strategien und Modelle in `audit_reports/15_strategy_proposals.md` (S1-S13 + Hypothesen-Tests: Konfluenz-2 = +2pp WR aber 4+ Modelle = Kontra-Signal; Regime-Richtungs-Matrix CHOP→nur SHORT; AIM1-Fade +9,5%/Trade auf Papier, nur Shadow; FIFO-Problem ist Selektion, nicht Tails).

**Step 4 (2026-07-03) ist gelaufen** — realisierte Bot-/Strategie-Ergebnisse aus der Live-DB: `audit_reports/14_bot_performance_db.md`. Kurzfassung: 82% von closed_ai_signals ist Migrations-Duplikat-Müll (357k Rows, Unique-Index fehlt); aktive Ära dedupliziert: AI gesamt +0,77%/Trade (Träger: MIS1-72H, EPD1, MIS1-168H, ROM1), klar negativ: AIM1, UFI1, BR-Familie, BB_1H; Classic gesamt −0,07%/Trade trotz 63% "WR" (Win=TP1-Touch ist als KPI irreführend); große Richtungs-Asymmetrien (EPD1 SHORT 76,5% vs LONG 50,2%).

**Step 2 (2026-07-03) ist gelaufen** — Ergebnisse/Beweise in `audit_reports/STEP2_DB_VERIFICATION.md`. Markierung hier: ✔ = live bestätigt · ✘ = widerlegt/entschärft · Details im Step-2-Report. Wichtigste neue Funde: Data-Ingestion-WS 6h tot bei grünem Watchdog (P2.47 live), Whale-Logger seit 18.4. tot, AIM1-Kalibrierung invertiert (conf>0.9 → 9,3% WR), Whitelist-Raw-Namen seit 19.4. eingefroren.

**Wichtiger Gesamt-Befund:** Das Repo ist erkennbar mitten in einer Sanierungswelle (viele saubere `# FIX:`-Kommentare). Der Großteil der schweren Findings ist **"Fix auf einer Vertragsseite gemacht, Gegenseite vergessen"** (Writer UTC-fixed / Reader naiv; ensure_schema migriert / 3_detectors legt schmal an; pretty_name im Analyzer / roh im Orchestrator). Vier strukturelle Root-Causes (unten, Abschnitt R) erzeugen zusammen ~60% aller Einzel-Findings — wer die zuerst löst, räumt breit auf.

---

## R — Strukturelle Root-Causes (zuerst lösen — je 1 Fix killt eine ganze Bug-Klasse)

- [ ] **R1 ✔(Step2: Partial-Kerze + Indikatoren darauf bewiesen) — Forming-Candle-Vertrag festnageln.** `1_data_ingestion.py:488-502` buffert **jede** WS-Kline ohne `k['x']`-Closed-Flag → jede `{sym}_{tf}`-Tabelle enthält die laufende Kerze; `2_indicator_engine.py:551-565` rechnet Indikatoren darauf und stempelt bei **:02 UND :32**. Folge: Look-ahead/Repaint in ~allen Strategien und ML-Bots. **Fix:** entweder nur `k['x']=true`-Kerzen in die Candle-Tabellen schreiben (separate Live-Tick-Tabelle für Bedarf), oder `is_closed`-Spalte + alle Reader schließen die laufende Kerze aus. `chart_data_service.py:247-249` macht es bereits richtig (Referenz). Betrifft: MIS1 (kritisch), RUB1, SRA1, AIM1, ATB1, alle 5 Classic-Strats, SMC 16/24/25/29. `[DB]`
- [ ] **R2 — Single-Source Fleet-/Schema-Definitionen.** (a) Prozessliste existiert doppelt und ist bereits um 4 Bots gedriftet: `dashboard.py:31-62` vs `main_watchdog.py:52-66` (Dashboard fehlen 26/27/28/29). (b) `CREATE TABLE` über ~10 Dateien verstreut mit Drift (`trade_cooldowns` ×4 mit WITH/WITHOUT TZ; `telegram_outbox` schmal in `3_detectors.py:103,141` ohne `image_path`); `ai_signals` (13 Writer) und `ml_predictions_master` (9 Writer) haben **gar keine DDL im Repo**. **Fix:** `core/fleet.py` (Name/Script/Group/Delays) + `docs/schema.sql` (pg_dump) bzw. `core/schema.py` mit Migrations-Runner beim Watchdog-Start.
- [ ] **R3 ✔(Step2: Session-TZ Europe/Bucharest; naive Spalten gemischt UTC/lokal) — Zentrale UTC-Politik.** Writer teils UTC-fixed, Reader naiv-lokal → TZ-Offset-Bugs in Cooldowns, Trade-Fenstern, Statistik (siehe P1-TZ, P2-TZ). **Fix:** `core/time.py: utc_now()`, alle `datetime.now()` ersetzen, `-c timezone=UTC` in den Pool-`options` (`core/database.py:42`), money-relevante Zeitspalten → `timestamptz`. Optional ruff-Rule `DTZ` (flake8-datetimez) im `pyproject`. `[DB]`
- [ ] **R4 — Leverage-vs-SL-Abgleich zentralisieren.** Nirgends abgeglichen: `21_btc_smc` 100x/1.2%-SL, `29_ufi1` 20x/~34%-SL → Liquidation lange vor dem SL. **Fix:** `core/trade_utils.py: cap_leverage_to_sl(sl_pct)` (z.B. `lev ≤ 0.5/sl_pct`), von allen signal-emittierenden Bots nutzen; zusätzlich SL-Distanz-Cap wie `calculate_smart_targets` (15%) auch im ROM1-Pfad.

---

## P0 — Geld-kritisch (sofort)

### Doppel-Trades bei Cornix (mehrere unabhängige Pfade)
- [ ] **P0.1 ~(Step2: 0 Retry-Doppel-Sends bisher; aber Upstream-Doppel-Generierung in Trading-Channels) Outbox ist at-least-once → doppelte Sends an den Cornix-Channel.** `4_telegram_bot.py:240-264` sendet → `mark_sent` → `commit`. Commit-Fehler nach erfolgreichem Send / Crash zwischen Send und Commit / `TimedOut` nach Annahme → Re-Send → Cornix eröffnet den Trade doppelt. **Fix:** `sending`-Status VOR dem Send committen; beim Restart stehen gebliebene `sending`-Rows für Trading-Channels in Dead-Letter statt Auto-Resend; `TimedOut` als "unknown outcome" behandeln. `[DB]`
- [ ] **P0.2 Kein Single-Instance-Guard auf dem Watchdog → doppelte Fleet.** `main_watchdog.py`: `taskkill /F` läuft nicht durch den SIGTERM-Handler → verwaiste Kinder; neuer Watchdog spawnt zweite Fleet → jedes Signal doppelt → Cornix doppelt. **Fix:** Named Mutex/Pidfile bei Watchdog- und Bot-Start; Windows Job Object `KILL_ON_JOB_CLOSE`; Orphan-Detection beim Start. `[DB]`
- [ ] **P0.3 ✔(Step2: 109 Self-Echo-Rows) Orchestrator konsumiert eigene ROM1-Posts.** `28_signal_orchestrator.py:386-421,524-537`: ROM1-Message enthält `Triggered by: MIS1-8H` → matcht die Bot-Patterns; Scan-SELECT hat keinen Channel-Filter → Self-Echo durch die ganze Pipeline; einziger Schutz ist der 4h-Cooldown, der erst NACH dem Send committed wird → Crash-Fenster = zweiter Trade. **Fix:** `channel_id != REGIME_TRADING_CHANNEL_ID` im SELECT; ROM1-Hard-Reject; Cooldown/Tracking VOR dem Send committen. `[DB]`

### Regime-Gating de facto abgeschaltet (Kern-Feature!)
- [ ] **P0.4 ✔(Step2, präzisiert: Raw-Namen-Rows seit 19.04. eingefroren → MIS+Channel-Bots gaten auf 2,5 Monate alten Stats) Whitelist-Gate läuft ins Leere (Bot-Name-Mismatch).** `28_signal_orchestrator.py:134-148,240-251,556` vs `27_bot_regime_analyzer.py:322`: Analyzer schreibt `pretty_name()`-normalisierte Keys (`MIS1-8h`, `FastInOut`), Orchestrator fragt mit **rohen** Namen (`MIS1-8H`, `Fast In And Out`) → case-sensitiver Lookup findet nie etwas → `(True, "no_whitelist_entry")` → **Signal immer durchgereicht**; Fallback + Regime-Auto-Close ebenso inert. Betrifft die ganze MIS-Familie + alle Channel-Fallback-Bots. **Fix:** `bot_name = pretty_name(bot_name)` direkt nach `identify_bot()` (und beim Insert in `orchestrator_open_trades`); Default-Open-Rate alarmieren. `[DB]`

### Leverage jenseits der Liquidation
- [ ] **P0.5 `21_btc_smc_strategy.py:31-35,199,238` — 100x Leverage mit 0.4–1.2% SL.** Isoliert liquidiert bei ~-0.9% *vor* dem SL; jeder Stop = -100% Margin. **Fix:** siehe R4, oder `DESIRED_LEVERAGE ≤ 25`.
- [ ] **P0.6 `29_ufi1_bot.py:194,244` — 20x mit ~34% SL** (`sl=swing_high*1.03`, Entry ~0.77·sh). Isoliert liquidiert bei ~+5%; die Backtest-"+0.83R" überleben 20x nicht. **Fix:** Leverage aus SL-Distanz (~1-2x) oder UFI1-Cap ≤3x.

### Kaputte Trades / stiller Datenverlust
- [ ] **P0.7 ✔(Step2: 5 aktive + 79 geschlossene) Leere-Zone-Interpolation erzeugt LONG-TPs UNTER dem Entry.** `strategies/strat_main_channel.py:70-87,115-132` + `strat_support_resistance.py:53-57,65-69`: bei 0 gefundenen Zonen ist `t1==0` ungeguarded → LONG TP1 = 0.75·Entry (SHORT: -25/-50/-75%). **Fix:** `if t1==0: return None` (oder Fixed-%-Fallback). `[DB]`
- [ ] **P0.8 Dashboard ohne Auth auf `0.0.0.0`.** `dashboard.py:1152` + Control-Endpoints `290-317`: jeder mit Port-5000-Zugriff kann `POST /api/system/stop_all` → Fleet persistent geparkt (übersteht Reboot). **Fix:** an `127.0.0.1` binden (+SSH-Tunnel) oder Token-Auth in `before_request` + Firewall. `[DB]` (Port-Erreichbarkeit am VPS prüfen)
- [ ] **P0.9 ✔(Step2: PK (symbol,open_time) vs ON CONFLICT (open_time); aktuell 0 Lücken dank REST-Catch-up) Nightly Gap-Filler ist stiller No-op.** `6_housekeeping.py:654-661`: PK ist `(symbol, open_time)`, INSERT nutzt `ON CONFLICT (open_time)` (kein passender Unique-Index → jeder Insert wirft) UND lässt `symbol` weg; Exception von `except:continue` verschluckt → Lücken werden nie gefüllt, Indikator-Invalidierung übersprungen. Das gesamte nächtliche Safety-Net existiert nicht. **Fix:** `INSERT (symbol, open_time, ...) ON CONFLICT (symbol, open_time) DO NOTHING`; Per-Gap-Fehler loggen. `[DB]`

### ML-Modelle trainieren/messen etwas anderes als die Bots handeln
- [ ] **P0.10 ✔(Step3: dasselbe Muster in 7/8 _X-Trainer-Familien bestätigt — siehe 13_x_ml_trainers.md, X-R1) "Backtest the detector, trade something else."** SMC-Modelle labeln auf idealisierten Fills, die die Live-Bots nie handeln: `smc_ml_trainer.py:128,159` (TD entry = Pivot-Close, erst 10 Bars später bekannt) und `:195-227` (fixe 1%/2% bzw. 2R-Geometrie) vs Live `calculate_smart_targets` (`25_smc_ml_sniper.py:131`). `predict_proba` + Schwellen (0.30/0.40) gelten für nie ausgeführte Trades. **Fix:** einen gemeinsamen Walk-Forward-Simulator bauen, der die **bot-eigenen** Setup-Funktionen bar-für-bar abspielt; darauf neu trainieren. `[DB]` (Shadow-Log-Kalibrierung)
- [ ] **P0.11 ✔(Step2: live 25,7% WR, n=35) UFI1 "+278R"-Claim nicht auf den Live-Bot übertragbar.** `29_ufi1_bot.py` vs `fib_backtest.py:252-262`: Backtest nutzt die **zukünftige globale** Fenster-Tiefstmarke zur Entry-Wahl (Look-ahead) und eine 5-Target-Trailing-Ladder; Live postet CMP-Entry (bis Wochen alt) + single TP1. **Fix:** Backtest = Walk-Forward mit `find_ufi1_setup`; Confirmation-Kerze muss aktuell sein (`j ≤ n-2`); Exit-Modell angleichen, bevor Zahlen zitiert werden. `[DB]`
- [ ] **P0.12 ✔✔(Step3: Split-Counts in beiden Live-Modellen = 0 für exakt diese 11 Features; Trainer hat identischen Bug — BT2-Datagrepper-for-ML.py:77-92) ABR1: 11 von 18 Features sind konstant 0 (bewiesen).** `18_ai_abr1_bot.py:112-177`: `expected_pta_cols` matcht die pandas_ta-Spaltennamen nie (`KAMA_9` vs `KAMA_9_2_30`, `TSI_12_7` vs `TSI_7_12_7`, `DCL_20` vs `DCL_20_20`, `BBL_20_2` vs `BBL_20_2.0_2.0`) → NaN → `fillna(0)`. Direkt aus `bt2_model_*.json` verifiziert: exakt diese 11 Features haben **0 Splits**. Der Trainer hatte denselben Bug → kein Skew, aber das Modell handelt real nur auf 7 Features (halbes Strategie-Signal fehlt). **Fix:** Spalten-Prefix-Matching (wie `14:197-211`), **beide Modelle neu trainieren**, Startup-Assertion "kein Feature konstant".
- [ ] **P0.13 ✔✔(Step2: Dummy-Overlap 2/22 bzw. 0/5; Kalibrierung invertiert, conf>0.9 → 9,3% WR — Bot pausieren!) AIM1 Master: Source-Identity-One-Hots sind für fast alle Live-Signale tot (bewiesen).** `15_ai_master_bot.py:220-273`: aus dem pkl extrahierte Feature-Liste kennt nur `ai_model_MSI1-*` (Typo/alte Schreibweise), `conv_bot_{5% Bot,Fast Bot,...}` — Live schreibt `MIS1-24H`, `ATB1`, `BB_1H`, `Fast In And Out` etc. → `reindex(fill_value=0)` nullt alle Identity-Dummies → das Meta-Modell kann Quellen nicht unterscheiden (sein Kernjob) → Out-of-Distribution. **Fix:** auf aktuelles Vokabular neu trainieren; Drift-Log für verworfene Dummy-Spalten. `[DB]`

---

## P1 — Hoch

### Telegram / Monitore
- [ ] **P1.1 Keine Staleness-TTL auf der Outbox.** `4_telegram_bot.py:183-194`: nach Downtime werden stundenalte Signale zu längst vergangenen Preisen rausgeblasen. **Fix:** `AND created_at > NOW()-INTERVAL '15 min'` für Signal-Channels, ältere `failed='expired'`. `[DB]`
- [ ] **P1.2 Trailing-SL in `5_trade_monitor.py:246-247` zieht nie nach** — für Level 2/3 wird der **alte** SL (`trade['sl']`) statt `targets[new_level-2]` übergeben (8_ai macht es richtig). Alle Multi-Target-PnL/Winrates systematisch falsch. `[DB]` (CRITICAL falls irgendetwas `active_trades_master.sl` an Cornix postet)
- [ ] **P1.3 Per-Channel-FIFO bricht bei transientem Sendefehler** (`4_telegram_bot.py:305-324`) → SL-Update kann vor seinem Entry-Signal ankommen. **Fix:** fehlgeschlagenen Channel für den Rest des Batches blocken.
- [ ] **P1.4 `:.6f`-Preisformatierung zerstört Sub-0.001-Coins** (`handlers/open_handler.py:104-110`) → gerundete/kollabierende TPs, Cornix rejected. **Fix:** signifikante Stellen / tickSize. `[DB]`
- [ ] **P1.5 ✘(Step2: Spalte ist INTEGER) `8_ai_trade_monitor.py:265` — `int > str` TypeError wenn `current_target_hit` TEXT ist** → ganze Monitor-Iteration stirbt → SL-Hits aller AI-Trades unerkannt. **Fix:** `old_targets_hit = int(targets_hit or 0)` mit try/except. `[DB]`

### Orchestrator / Regime
- [ ] **P1.6 `sent=FALSE`-Filter racet gegen den Dispatcher** (`28:524-537` vs `4:263,338`) → zwischen zwei Pässen gesendete Signale fallen aus dem SELECT → nie gegated, kein Log. **Fix:** `sent/failed`-Bedingung raus, Neuheit über `id`-Cursor. `[DB]`
- [ ] **P1.7 Forward-Pipeline nicht atomar; Batch-Cursor erst am Pass-Ende** (`28:597-627`) → Crash nach Send = Trade bei Cornix ohne Tracking; Exception bei Row 5/10 = Rows 1-4 werden erneut gepostet. **Fix:** DB-Writes in einer Txn zuerst, Outbox-Insert zuletzt, Cursor + Exception pro Row. `[DB]`
- [ ] **P1.8 `sync_closed_trades` matcht fremde Trades** (`28:879-925`, kein model-Filter, kein ORDER BY, 720h-Fenster) → falsche ROM1-Outcomes + vorzeitiger Verlust des Opposite-Schutzes (Hedge/Doppel-Exposure). **Fix:** nur `closed_ai_signals WHERE model='ROM1'`, ±60s gegen `open_time`, ORDER BY. `[DB]`
- [ ] **P1.9 Regime-Close löscht ALLE offenen Trades aller Bots auf coin+direction** (`28:672-817`, kein model/strategy-Filter) → fremde Verluste als neutral zensiert, korreliert mit Regime-Wechseln → Whitelist-Winrates nach oben gebiast (das Geld-Gate!). **Fix:** nur `model='ROM1'`. `[DB]`
- [ ] **P1.10 Spec-Drift: Doku sagt "reiner Signal-Router", Code baut eigene Trades** (`docs/REGIME_ORCHESTRATOR.md:18` vs `28:288-421`). ROM1 verwirft Original-Entry/SL/Targets → Gating-Statistik ≠ Ausführungs-Statistik. **Fix:** Doku auf v6, Risiko dokumentieren.

### Datenpipeline (Integrität)
- [ ] **P1.11 WS-Buffer keyed by `(symbol, timeframe)` verliert die finale Kerzen-Aktualisierung** an jeder Kerzen-Grenze (`1_data_ingestion.py:494-502`) → gespeicherte "Closed"-Kerze steht bis zum 12h-REST-Catch-up leicht falsch. **Fix:** Key `(sym, tf, open_time)` oder Flush-through bei `k['x']`. `[DB]`
- [ ] **P1.12 ✔(Step2: poc 149 distinct/5000 alte Rows, support 236; trendline_price per-row ok) Whole-Window-Indikatoren (Trendline/POC/HVN/S-R/Fib) als Konstante auf jede Zeile broadcastet** (`2_indicator_engine.py:435-467,562-565`) → Look-ahead in der gespeicherten Historie; Werte fensterlängen-abhängig. **Fix:** nur für die letzte Zeile schreiben (NULL sonst), als "as-of-now" dokumentieren. `[DB]`
- [ ] **P1.13 ~(Step2: aktuell 0 ma_200=0-Rows; Risiko bleibt für Neu-Listings) `fillna(0)` auf Warm-up-Fenstern schreibt erfundene Indikatorwerte** (`2_indicator_engine.py:325,384-403`) → junge Coins: `MA_200=0` permanent → `close>MA_200` trivial wahr → Fehlsignale genau auf illiquiden Neu-Listings. **Fix:** NaN/NULL fließen lassen (wie KAMA es tut). `[DB]`

### Klassische Strategien
- [ ] **P1.14 SHORT-"Headroom"-Check ist ein vorzeichenverdrehter No-op** (`strat_fast_in_out.py:74`, `strat_5_percent.py:97`): `close > support*0.95` ist quasi immer wahr → SHORT hat keinen Headroom-Guard. **Fix:** `close > support*1.05`. `[DB]`
- [ ] **P1.15 Ein schlechter Coin killt den ganzen Detector-Prozess** (`3_detectors.py:186-233`, Strategie-Calls unprotected, `main()` fängt nur `FileNotFoundError`) → halbe Coin-Liste ungescannt bis coins.json gefixt. **Fix:** per-Coin try/except + rollback, breites except mit Backoff in main().
- [ ] **P1.16 Volume Indicator hat keinen Cooldown** (`strat_volume_indicator.py:68-100`) → bis zu 5 Tage alter Spike refeuert alle 30 min (Serien-Reentry). **Fix:** `check_cooldown/update_cooldown` (12-24h) oder Dedupe auf Spike-Timestamp. `[DB]`

### AI-Bots (Look-ahead / Skew / Robustheit)
- [ ] **P1.17 MIS1 predicted auf der laufenden Kerze mit stale Partial-Indikatoren** (`11_ai_mis_bot.py:228,238-242`) → strukturell verzerrte Volume-Features auf **jeder** Prediction; getunte Schwellen bedeutungslos. ATS (`12:143-147`) macht es mit `-2` richtig. **Fix:** `iloc[-2:-1]` bzw. `open_time < date_trunc('hour', NOW())`. `[DB]`
- [ ] **P1.18 ✘(Step3: alle 8 pkls haben identische 67 feature_names_in_ — Introspektion; .values-Fragilität bleibt P3) MIS1: ein Feature-Set für alle 8 Modelle + `.values` deaktiviert sklearn-Namensvalidierung** (`11:205-209,250,259`) → nach Teil-Retrain permutierte Features, `except:pass` verschluckt den Shape-Error. **Fix:** je Modell `df[model.feature_names_in_]`, Exception loggen.
- [ ] **P1.19 RUB1 predicted auf Forming-Candle-Indikatoren** (`13_ai_rub_bot.py:90-97,117-131`) — LIMIT 1 = offene Kerze aus ~2 min Daten. **Fix:** closed-candle-Filter, `curr_close` aus derselben Kerze. `[DB]`
- [ ] **P1.20 SRA1: bedingt fehlende ATR-Features + keine per-Trade-Isolation** (`9_ai_sr_bot.py:135-143,268-305`) → 35 statt 38 Spalten → predict wirft → ganze Iteration bricht + rollback verwirft Shadow-Inserts; wiederholt sich alle 5 min für 60 min. **Fix:** ATR-Features immer als NaN emittieren (XGB kann NaN), per-Trade try/except. `[DB]`
- [ ] **P1.21 AIM1: Indikator-Features + Close aus der laufenden Stunde** (`15:391,423-431`, `open_time <= floor('h')`). **Fix:** `open_time < join_time`. `[DB]`
- [ ] **P1.22 ATB1: ML-Features auf 3-min-alter Forming-Candle** (`14:228-233,613-625`, `row=df.iloc[-1]` nicht gesliced) → `vol_ratio` ~1/20 der Trainingsskala. **Fix:** Features auf `df_90d.iloc[:-1]`, `last_close` separat. `[DB]`
- [ ] **P1.23 ATB1: aborted transaction vergiftet den Rest des 538-Coin-Scans** (`14:612-614,761-762`, kein rollback im per-Coin-except, nicht autocommit). **Fix:** `conn.rollback()` im except oder `autocommit=True`.
- [ ] **P1.24 QM/Pivot-Detection auf der Forming-Candle ohne Confirmation** (`24_quasimodo_bot.py:110-111` vs Trainer `183`; scipy `argrelextrema` mode='clip' lässt Kanten-Pivots durch) → Repaint, Trainer-Skew. **Fix:** Forming-Candle droppen + Pivots mit `index > len-1-PIVOT_WINDOW` verwerfen. `[DB]`
- [ ] **P1.25 TD-Sniper: Trainer-Entry ist Hindsight (Pivot-Close), Live-Geometrie völlig anders** (`25:203-241` vs `smc_ml_trainer.py:126-149`); Schwellen hardcoded statt aus pkl. **Fix:** siehe P0.10, Schwellen aus pkl laden. `[DB]`
- [ ] **P1.26 ✘(Step2: 83 SMC_*_FVG-Cooldown-Rows — Pfad feuert) SMC 16 FVG-Entry ist unerreichbar (Dead-Code)** (`16_smc_forex_metals_bot.py:159,418,446`): Mitigation-Scan inkludiert die aktuelle Kerze, Trigger nutzt dasselbe Prädikat → schließen sich aus → Bot emittiert nur STRUCTURE-Signale. **Fix:** `range(fvg['index']+1, len(df)-1)`. `[DB]` (Beweis: 0 `SMC_FVG`-Cooldown-Rows)
- [ ] **P1.27 SMC 16 entscheidet auf der Forming-Candle** (beide Datenquellen; `16:329-344,131-134`); forming 1d/1w hält Bedingung tagelang → 12h-Cooldown → Refire die ganze Woche. **Fix:** `iloc[:-1]`, Cooldown ≥ Kerzendauer.
- [ ] **P1.28 ABR1: 11/18-Features-Bug** → siehe **P0.12** (Schweregrad-Einordnung dort).

### Trainer / Backtest-Validität
- [ ] **P1.29 Random `train_test_split` auf Zeitreihen + überlappende Duplikate = Kontamination; "optimal threshold" auf dem Test-Set gewählt** (`qm_ml_trainer.py:261,290-325`, `smc_ml_trainer.py:262,276-314`) → optimistisch verzerrter Operating Point im pkl. **Fix:** chronologischer Split mit Purge-Gap, Schwelle auf Validation-Slice.
- [ ] **P1.30 QM-Fill-Logik löscht garantierte Verlierer + vergibt Same-Candle-TP-Wins** (`qm_ml_trainer.py:121-179`, `qm_backtest.py:167-184`) → Label-Verteilung nach oben verschoben. **Fix:** fill-then-stop konservativ, kein TP-Win auf der Entry-Kerze.
- [ ] **P1.31 ✘/~(Step2: 0/529 Coins ohne Tabellen — Truncation-Trigger aktuell nicht gegeben; Code-Bug bleibt) Silent-Exception + Pool-Leak → Trainer laufen still auf trunkiertem Coin-Universum** (`qm_ml_trainer.py:67-94` etc.): fehlende Indikator-Tabelle → `except: return empty` leakt Conn; nach 8 Leaks Pool erschöpft → **jeder** weitere Coin still übersprungen → Modell auf 0-8 Coins trainiert und **über das Produktions-pkl gespeichert**. **Fix:** `try/finally: conn.close()`, Skips loggen, bei zu wenig Coins abbrechen. `[DB]`

### Core / Infra / Dashboard
- [ ] **P1.32 `PooledConnection.close()` nicht idempotent** (`core/database.py:83-95`) → Double-Close vergiftet den Pool / rollbacked die Transaktion eines anderen Threads (HOTFIX_README dokumentiert genau diesen Vorfall). **Fix:** `_returned`-Flag; im Error-Pfad `putconn(conn, close=True)`.
- [ ] **P1.33 Pool-Slot-Leak wenn `rollback()` auf toter Connection wirft** (`core/database.py:86-95`) → nach DB-Restart erschöpft der Pool je Prozess dauerhaft → Bot "healthy", produziert nichts. **Fix:** `putconn(conn, close=True)` im except; Liveness-Check in `getconn`. `[DB]`
- [ ] **P1.34 ~(Step2: max_connections=200) Fleet-Connection-Budget** (`core/database.py:22-23`): 27 × maxconn 8 + ProcessPool-Worker > 216 vs Default `max_connections=100`; Crash-Restart-Burst → "too many clients" → Restart-Storm. **Fix:** MIN 0-1/MAX 2-3 env-overridable, oder pgBouncer. `[DB]`
- [ ] **P1.35 `core/update_model.py:35` überschreibt `.pkl`/`.joblib` in-place und zerstört das Original** (`replace(".model", ...)` ist No-op für `*_model.pkl`). **Fix:** `splitext`-Name + Refuse wenn `new==old`.
- [ ] **P1.36 Telegram-Permission-System fällt bei Config-Load-Fehler OPEN auf** (`core/bot_utils.py:12-18`, `{"*":["*"]}`; `open()` ohne `encoding="utf-8"` → cp1252-Crash auf Emoji). **Fix:** fail-closed (deny-all) + Alert, utf-8.
- [ ] **P1.37 Watchdog-Backoff `time.sleep()` friert die Monitor-Schleife ein** (`main_watchdog.py:299-303`, bis 900s) → während dessen keine anderen Restarts/Park/Dashboard-Checks; nach Sleep `start_process` ohne `is_parked`-Recheck. **Fix:** per-Prozess `not_before`-Timestamp statt Sleep.
- [ ] **P1.38 Dashboard-Prozessliste driftet + CSRF + Log-Streaming blockiert Windows-Rotation + `/api/status` 25 psutil-Sweeps/Tab/6s.** `dashboard.py:31-62,290-317,244-256,104-179` — siehe R2 (Liste) und die vier Einzel-Fixes: CSRF-Origin-Check, Log-Streaming mit re-open statt offenem Handle, ein `process_iter`-Sweep + `cpu_percent(interval=None)` + Server-Cache.

### Market-Intelligence
- [ ] **P1.39 Pump/Dump-Timestamp-Fix unvollständig** (`10_pump_dump_detector.py:522-529,552-558`): Volume-Explosion + ML-Features noch index-basiert → nach Restart falsche "VOLUME EXPLOSION"-Alerts + schiefe ML-Features. **Fix:** über `_find_bucket_before/range` routen.
- [ ] **P1.40 `pump_dump_events`: unconditional CREATE+INSERT pro Symbol pro 10s-Tick** (`10:569-578`) → ~108 stmt/s, ~4.6M Rows/Tag (rsi/tsi-Spalten nie befüllt). **Fix:** CREATE einmalig, Insert samplen/batchen. `[DB]`
- [ ] **P1.41 Shadow-Inserts in `ml_predictions_master` ohne Cooldown** (`10:625-635`) → bis 8640 Rows/Tag/Symbol, vom Market-Tracker als "opened signal" gezählt. **Fix:** per-Symbol Shadow-Cooldown; Consumer filtern `posted=TRUE`. `[DB]`
- [ ] **P1.42 ✔✔(Step2: 49/529 Symbole; Logger schreibt seit 18.04. gar keine Files mehr) Whale-Logger: 538 aggTrade-Streams auf einer Futures-WS-Connection** (`19:334-336`, fapi-Cap ~200/Conn) → ~340 Symbole still nicht geliefert. **Fix:** in 3 Connections sharden. `[DB]` (Whale-Files vs coins.json)
- [ ] **P1.43 Market-Tracker: Pool-Leak bei Query-Fehler + fehlender rollback** (`23:395-429,749-831`) → 1 Leak/Stunde → nach ~8h alle Tracker-Jobs tot bis Restart. **Fix:** `try/finally close`, `rollback` vor Fallback. `[DB]`
- [ ] **P1.44 Market-Tracker "Opened"-Counts doppeln AI-Trades + zählen Shadow-Predictions** (`23:399-425`) → verzerrt jede Per-Bot-Statistik (die Entscheidungsgrundlage). **Fix:** `posted=TRUE`-Filter, Opens nur aus `ai_signals`+`closed_ai_signals`. `[DB]`

---

## P2 — Mittel (Auswahl der wirkungsvollsten; Details in den Agent-Reports)

### Timezone (nach R3 abarbeiten)
- [ ] **P2.1 Cooldown-Circuit-Breaker der Classic-Strats vergleicht naive Lokalzeit gegen UTC-`posted`** (`strat_fast_in_out.py:42-48`, `strat_5_percent.py:25-29`) → in CEST deckt das 3h-Fenster nur 1-2h. `[DB]`
- [ ] **P2.2 ✔aufgelöst(Step2: live gewann die timestamptz-Variante) `trade_cooldowns` DDL-Drift ×4 (WITH vs WITHOUT TZ)** — Bootstrap-Reihenfolge entscheidet Cooldown-Semantik (`26:194-200` vs `11/24/25`). `[DB]`
- [ ] **P2.3 `active_trades_master.time/posted` naiv-lokal geschrieben** (`3_detectors.py:54,117`), mit `NOW()`/aware-UTC verglichen → 60-min-Fenster wird 2h+. `[DB]`
- [ ] **P2.4 `closed_ai_signals.close_time`: `NOW()` vs Python-UTC gemischt** über 3 Writer (`8:247`, `6:201`, `28:729`). `[DB]`
- [ ] **P2.5 `update_cooldown` schreibt `NOW()` (Session-TZ) in naive Spalte, Reader liest als UTC** (`core/market_utils.py:104-135`) — R3 löst das zentral. `[DB]`
- [ ] **P2.6 `5_trade_monitor.posted`: tz-aware UTC in `TIMESTAMP WITHOUT TIME ZONE`** (`5:22-25,59`) → Fix wirkungslos falls Session-TZ ≠ UTC. `[DB]`

### Monitore / Datenintegrität
- [ ] **P2.7 Monitore inspizieren nur die neueste 5m-Kerze** (`5:152-176`, `8:82-110`, `ORDER BY open_time DESC LIMIT 1`) → SL/TP-Hits während Downtime permanent verpasst. **Fix:** `last_checked_open_time`, vorwärts scannen. `[DB]`
- [ ] **P2.8 Unguarded Close-Race** (`5:31-65`, `8:245-276`) → Doppel-Rows in Closed-Tabellen, Lost Updates. **Fix:** `DELETE ... RETURNING` first, Insert nur bei Treffer. `[DB]`
- [ ] **P2.9 SHORT-Trade mit `sl=0` sofort "ausgestoppt" bei Preis 0 (+100% PnL)** (`5:216-225`). **Fix:** Guard `sl>0`. `[DB]`
- [ ] **P2.10 Kein `FOR UPDATE SKIP LOCKED`/Singleton auf der Outbox** (`4:183-194`) → zwei Consumer würden alles doppelt senden.
- [ ] **P2.11 Permanent-failing Messages nach 3 Versuchen still verworfen, kein Alert** (`4:22,124-144`) → verlorenes Cornix-Signal unsichtbar. **Fix:** Retry ohne parse_mode, Operator-Alert. `[DB]`

### Indikator-Engine / Ingestion / Housekeeping
- [ ] **P2.12 ✔(Step2: gespeichert==ewm(span), Δ zu Wilder ø4,8 Punkte) RSI ist kein Wilder-RSI** (`2_indicator_engine.py:336-337`, `ewm(span)` statt `alpha=1/period`) → RSI_14 ≈ Wilder-7-8; Schwellen 70/30 feuern zu oft (ATR im selben File korrekt). **Fix:** bewusste Migration. `[DB]`
- [ ] **P2.13 Indikator-Engine rollt Fenster über Candle-Gaps ohne Continuity-Check** (`2:551-561`). **Fix:** Gap-Check über Lookback, bei Verletzung skip+log. `[DB]`
- [ ] **P2.14 `fetch_ohlcv_batch` kann ewig loopen; 418-Ban-Handling hämmert in den Ban** (`1:94-120`) → ein stuck Symbol blockt alle 12h-Catch-ups. **Fix:** Max-Retry, 418 → ≥120s exponentiell.
- [ ] **P2.15 Coin-Liste beim Prozessstart eingefroren** (`1:579-591`, `chart_data_service.py:356-374`) → neu gelistete Coins bekommen bis Restart keine Daten. `[DB]`
- [ ] **P2.16 Zwei Prozesse schreiben `coins.json` mit verschiedenen Filtern, non-atomar** (`1:31-56` inkl. Quarterlies vs `6:24-47` PERPETUAL). **Fix:** ein Writer via Core, tmp+os.replace. `[DB]`
- [ ] **P2.17 Delisted-Cleanup schließt Trades auf jedem Symbol nicht in coins.json** (`6:128,186`) inkl. Metals/Forex/ETHBTC → nächtliche Falsch-Closes. **Fix:** auf Binance-Perp-Shape beschränken. `[DB]`
- [ ] **P2.18 Housekeeping-REST ohne 429/418-Handling** (`6:508-522`) → nach dem Gap-Filler-Fix Burst → 418-IP-Ban trifft auch Trading-Endpoints. **Fix:** Retry-After/Backoff spiegeln.
- [ ] **P2.19 Indikator-Zyklus riskiert das 30-min-Budget** (`2:585-626`, WMA via Python-Lambda-`apply`, KAMA Python-Loop, ProcessPool je TF neu) → Überlauf skippt still den nächsten Trigger. **Fix:** WMA vektorisieren (`np.convolve`/`sliding_window_view`), ein Executor/Zyklus, WARN bei >25min. `[DB]`
- [ ] **P2.20 chart_data_service: kein Message-Watchdog + Gap-Handling** (`chart_data_service.py:184-232`); 12MB-JSON-Snapshot synchron auf dem Event-Loop alle 60s (`102-119`). **Fix:** `asyncio.wait_for(recv,120)`, `to_thread` für dump, Intervall 300s.

### Orchestrator/Regime (Rest)
- [ ] **P2.21 TZ-Mix Cooldown/Outbox-Fenster** (`28:521-536`) — R3. `[DB]`
- [ ] **P2.22 Analyzer attribuiert auf RAW `regime_history`, Gating auf debounced `regime_current`** (`27:227-236` vs `regime_logic.py:263-422`) → um Übergänge falsche Regime-Zuordnung; Backfill-Look-ahead. `[DB]`
- [ ] **P2.23 ✔(Step2: TRANSITION 44,5% Anteil, 2,9 Raw-Wechsel/Tag) "Unreliable"-Heuristik zählt RAW-Flaps** (`28:177-191`) → System kann dauerhaft im groben Overall-Fallback hängen (Kern-Feature deaktiviert). `[DB]`
- [ ] **P2.24 Regime-Wechsel während Orchestrator-Downtime nie nachgeholt** (`28:76-77,949-952`, In-Memory-State). **Fix:** beim Start alle OPEN-Trades gegen aktuelle Whitelist prüfen. `[DB]`
- [ ] **P2.25 ✔✔(Step2: Raw-Namen-Rows computed_at=19.04. — genau die, die der Orchestrator liest) Stale `bot_regime_whitelist`-Rows nie bereinigt** (`27:747-793` nur Perf-Tabelle) → Orchestrator gated (via P0.4-Rohnamen) auf monatealten Einträgen. **Fix:** Cleanup ausdehnen + `computed_at`-Staleness-Gate. `[DB]`
- [ ] **P2.26 Kein Same-Direction-Open-Check** (`28:272-284`) → nach 4h-Cooldown stapelt ROM1 Positionen auf denselben Coin. `[DB]`
- [ ] **P2.27 ✔(Step2: p90=17,9%, max 65%; 20/133 >15%) ROM1-SL ohne Distanz-Cap** (`28:355-366`) → nächste S/R-Zone 30-50% weg, bei 20x jenseits Liquidation — R4. `[DB]`
- [ ] **P2.28 60s-Detection-Fenster + `start_delay=175`** (`28:35`) → jeder Restart wirft ≥3 min Signalstrom kommentarlos weg. **Fix:** Fenster 5-10 min + `stale_signal`-Log. `[DB]`

### Weitere AI/SMC/Classic (Auswahl)
- [ ] **P2.29 `get_hvn_and_sr_levels` liest 95d ohne `ORDER BY`** (`core/trade_utils.py:263-276`, genutzt von SRA1/ATS1/RUB1 für SL/TP) → argrelextrema auf ggf. unsortierten Rows → Phantom-Extrema als SL/TP-Preise. **Fix:** `ORDER BY open_time ASC` (eine Zeile). `[DB]`
- [ ] **P2.30 SRA1 loggt `posted=True` auch wenn Cooldown den Post unterdrückt hat** (`9:163-164,278-283`) → Phantom-Posts in der Performance-Auswertung. `[DB]`
- [ ] **P2.31 ✔(Step2: targets_hit bis 21) Subscribers sehen TP1-3/1-5, Monitor scored bis 10-20 Targets** (9/11/12/13) → Live-Statistik ≠ Cornix-Realität. **Fix:** exakt die publizierten Targets speichern. `[DB]`
- [ ] **P2.32 MIS1 `autocommit=True`** (`11:190`) → Outbox/ai_signals/master-log-Inserts nicht atomar. **Fix:** autocommit weg, ein commit. `[DB]`
- [ ] **P2.33 MIS1 Best-Candidate vergleicht rohe Probabilities über verschieden kalibrierte Modelle** (`11:252-271`) → unter-Schwelle-Kandidat verdrängt über-Schwelle-Signal. **Fix:** nach `prob - threshold` ranken.
- [ ] **P2.34 MIS1 `fillna(0)` reinigt kein `inf` aus Zero-Volume-Divisionen** (`11:131-133`). **Fix:** `replace([inf,-inf],nan)`.
- [ ] **P2.35 AIM1 5-min-Fenster ohne Catch-up + Kontext-Features zählen den Kandidaten selbst + `conv_signal`-Dedup-Key kollidiert** (`15:299,303-316,321-331`). Fixes je: Fenster 60 min; Kandidat/AIM1 ausschließen; distinkte signal_types. `[DB]`
- [ ] **P2.36 ATB1 "unknown"-State-Break-Trigger reaktiviert** (`14:52-56,660-673`) → State-Loss = Massen-Event-Flood (Comment gibt den Bug offen zu). **Fix:** unknown = observe-only. `[DB]`
- [ ] **P2.37 ATB1 Main-Loop fängt nur KeyboardInterrupt** (`14:593,764`) → jede Scan-Exception killt den Prozess + leakt Conn. **Fix:** try/finally close, breites except+Backoff.
- [ ] **P2.38 ✔✔entwarnt(Step2: LONG 67%/SHORT 59% WR; Step3: LabelEncoder+meta.json+num_class=3 — Index 0 korrekt) ABR1: SUCCESS_CLASS_IDX + SHORT-Label-Semantik unverifizierbar** (`18:41-54`, Trainer nirgends auf der Maschine). **Fix:** LabelEncoder.classes_ im Modell persistieren + Load-Assert. `[DB]` (Outcome-vs-Confidence-Join ist der entscheidende Test)
- [ ] **P2.39 Break-and-Retest (25) prüft nur `peak_idx[-2]`** (`25:250-264`) → bei frischem Retest falsches Level; Feature-Timing Breakout vs Retest. `[DB]`
- [ ] **P2.40 Funding "Extreme"-Schwelle 75% positiv feuert im Normalzustand** (`20:164-186`, Baseline +0.01%). **Fix:** 95/85, Magnitude oder Transitions. `[DB]`
- [ ] **P2.41 Market-Tracker: Full-History-Load pro Stunde + alle "async"-Jobs synchron + Regime-Fit-Query ohne rollback + Chunker splittet Über-Block nicht** (`23:755-776,64-183,646-712,1180-1231`). `[DB]`
- [ ] **P2.42 Empty-zone / Volume-Spike-Klassifikation / HVN-tick-size-Abhängigkeit** (`strat_volume_indicator.py:22-63`): ältester Spike gewinnt, `i==0` immer Sell, HVN-Gate degeneriert je Tick-Size. **Fix:** rückwärts iterieren, Preise binnen. `[DB]`
- [ ] **P2.43 5% SHORT nutzt `ema_12<ema_55` wo LONG `ema_21>ema_55`** (`strat_5_percent.py:86`, wahrscheinlich Typo) + `REQUIRED_COLUMNS` deckt `ema_200/wma_21/wma_26` nicht (`11-14`).
- [ ] **P2.44 538 serielle Binance-HTTP-Calls pro Detector-Zyklus** (`3:199-201`) + Volume-Strat liest 90d×30m als ersten Gate (`strat_volume_indicator.py:14-19`). **Fix:** Batch-`ticker/price`, Guards umsortieren. `[DB]`
- [ ] **P2.45 SMC/Mayank Weekend-Refire + fehlende FVG-Age-Limits + fehlende SL/RR-Checks** (`16:483,354`; `17:234,246-253`). `[DB]`
- [ ] **P2.46 `21_btc_smc` ohne Cooldown/Dedupe** (`21:121-123,264`) → bei Filler-Lag Doppelsignal 1h auseinander. `[DB]`
- [ ] **P2.47 ✔✔(Step2: Ingestion-WS 6h tot bei grünem Watchdog, Fleet handelte auf Stale-Daten) Watchdog ohne Hang-Detection + kein `statement_timeout`/keepalives** (`main_watchdog.py:294-304`, `core/database.py:42`) → wedged Bot bleibt "grün". **Fix:** statement_timeout+keepalives, Heartbeat. `[DB]`
- [ ] **P2.48 Windows `terminate()` = harter Kill** (`main_watchdog.py:169-181`) → kein Graceful Shutdown; ProcessPool-Worker der Engine überleben Parent-Kill → Doppel-Compute-Fenster. **Fix:** CTRL_BREAK_EVENT/Job-Object.
- [ ] **P2.49 `atomic_write_json` scheitert auf Windows bei offenem Reader** (`core/state_utils.py:50`) → Update still verworfen; fixer `.tmp`-Name → Korruption. **Fix:** unique tmp, retry.
- [ ] **P2.50 ✔erledigt(Step2: extract+refresh gegen Live-DB, 24 Fixtures/Goldens, verify grün) Regression-Guard schützt aktuell nichts** (`tools/regression_guard/guard.py:128-137`, unarmed → pass; golden-delete → stiller Disarm). **Fix:** extract+refresh (Step 2!); harter Fail wenn Manifest existiert aber Goldens fehlen. `[DB]`

---

## P3 — Niedrig / Kosmetik / Aufräumen (gesammelt)

- [ ] **P3.1 Duplikate/Dead-Code:** `tools/db_schema_analysis.py` (stale, kann nicht mal laufen) löschen (Root ist kanonisch); `load_coins` ×6 mit Semantik-Drift auf Core ziehen; `3_detectors.py:53-106` tote Signal-Writer entfernen; `_apply_keepalive` ×2, TIMEFRAMES-Redeklaration in Housekeeping.
- [ ] **P3.2 Unrotierte Logs:** `2_indicator_engine.py` (`indicator_calculation.log`), `main_watchdog.py` (`watchdog.log`), `dashboard.log`-Pipe → `setup_logging()` bzw. Truncate im Housekeeping.
- [ ] **P3.3 SQL-Identifier-Hygiene:** ~40 f-String-Tabellennamen aus coins.json — eine zentrale `re.fullmatch(r'[A-Z0-9]+', symbol)`-Validierung in `load_coins` schließt alle. (Kein aktiver Injection-Pfad, aber Second-Order-Risiko in `28:312` aus geparstem Message-Text.)
- [ ] **P3.4 requirements.txt vollständig ungepinnt** → `pip freeze` als `requirements.lock.txt`, mindestens Major-Pins (pandas/PTB/xgboost). Modell-pkls: `xgb.__version__` im Artefakt + Load-Assert.
- [ ] **P3.5 Sub-Cent-Formatierung/`@None`-Attribution/Blocking-IO in async** (`19:207,212`; `open_handler.py:95-96,20`); `describe_project.py` GNU-`tree`-Annahme + Full-Source-Dump (Info-Leak).
- [ ] **P3.6 Fees in Backtests deklariert aber nie angewandt** (`smc_pattern_backtester.py:20`); Survivorship-Bias (heutige coins.json über 1-2J); kein Kapital/Concurrency-Modell (summierte parallele Positionen); `bfill()`-Leak in Trainern; Thresholds im pkl aber Bots hardcoden. `[DB]` (delisted-Tabellen noch da?)
- [ ] **P3.7 Coin-Level-Exceptions auf DEBUG** (`24:280-281`, `25:306-307`) → unsichtbar; 29 macht es richtig (ERROR+exc_info+rollback). Angleichen.
- [ ] **P3.8 matplotlib-Backend nur in 16 auf `Agg`** (17/24/25 ohne) → würde headless crashen. Eine Zeile je Bot.
- [ ] **P3.9 Cornix-Double-Parse-Risiko:** 24/25/29 inserten Plain-Cornix-Block UND zweite HTML-Message mit identischem Block in denselben Channel — falls Cornix beide parsed, Doppelausführung. `16` bettet in eine Message (safe). Prüfen. `[DB]`
- [ ] **P3.10 Diverse Spec-Drift-Doku-Punkte** (`REGIME_ORCHESTRATOR.md`: regime_current-Init, ↑/↓-Marker nie implementiert, Fallback-Rate fehlt im Status-Post); Scheduler-Kommentare vs Code in ABR1/ATS/RUB; `ml_predictions_master.trade_id` immer 0 (tote Spalte).
- [ ] **P3.11 Chart-Verzeichnisse Growth** (`7:27-28`, `22:29-30`) — prüfen ob Housekeeping genau diese Dirs räumt.
- [ ] **P3.12 Preis-Spalten `REAL` (float4)** in `active_trades_master` + Indikator-Tabellen → Präzisionsverlust bei Sub-Cent-Coins. `[DB]`

---

## Step 2 — Live-DB-Validierungskatalog (VPS + DB-Zugriff)

Diese Queries bestätigen/messen die `[DB]`-Punkte und ziehen die **Strategie-Ergebnisse** in den Kontext, um jede Strategie gegen Herz und Nieren zu prüfen.

**Fundament (entscheidet die Schwere vieler Findings):**
1. `SHOW timezone;` + `SELECT NOW(), NOW() AT TIME ZONE 'UTC';` + VPS-OS-TZ → alle TZ-Findings (R3, P2.1-2.6).
2. `\d trade_cooldowns` · `\d active_trades_master` · `\d closed_ai_signals` · `\d telegram_outbox` · `\d ai_signals` · `\d ml_predictions_master` → Spaltentypen (TEXT vs INT bei `current_target_hit` → P1.5; `image_path` vorhanden → R2/P2; `posted` timestamp vs bool), Constraints/Indexe, Unique-Backstops.
3. `SHOW max_connections;` + `SELECT count(*), application_name FROM pg_stat_activity GROUP BY 2;` → P1.34; idle-in-transaction/stale Sessions → P1.33/P2.47.
4. `SELECT MAX(open_time) FROM "BTCUSDT_1h_indicators";` bei :35 Wall-Clock + `"BTCUSDT_1d"` intraday → beweist R1/Forming-Candle live.

**Doppel-Trade-/Duplikat-Beweise:**
5. Identische `message` in `telegram_outbox` zweimal binnen Minuten (`attempts>0 AND sent=TRUE` = Smoking Gun) → P0.1.
6. `suppressed_signals` mit `original_outbox_id` → `channel_id=CH_REGIME_TRADING` → P0.3 Self-Echo.
7. `orchestrator_open_trades`: `coin+direction`-Duplikate mit Zeitüberlappung → P2.26; OPEN >30d → Lifecycle-Löcher.
8. Watchdog-Logs: je zwei Orchestrator/Bot-Prozesse gleichzeitig → P0.2.

**Regime-Gating-Wirksamkeit (Kern-Feature):**
9. `SELECT DISTINCT bot_name FROM bot_regime_whitelist` vs `orchestrator_open_trades` vs `suppressed_signals(reason,bot_name)` + `MIN/MAX(computed_at)` → beweist P0.4-Mismatch + P2.25 stale Keys.
10. `wl_reason`-Verteilung 30 Tage: % der Forwards über echte 4D-Entscheidung vs `no_whitelist_entry`/Fallback → P0.4/Cross-Cutting.
11. `regime_history` 2h-Fenster `COUNT(DISTINCT regime)` über 30 Tage → wie oft `reliable=False`? + TRANSITION-Anteil → P2.23.

**Strategie-Herz-und-Nieren (der eigentliche Step-2-Zweck):**
12. **Kalibrierung pro Modell-Tag** (SRA1, MIS1-8/24/72/168H, ATS1, RUB1, QM, TD/BB, ABR1 LONG/SHORT, ROM1): `ml_predictions_master.confidence` (shadow+posted) gebucketet gegen realisiertes Outcome aus `closed_ai_signals`. Unkorreliert ⇒ Forming-Candle/Skew-Findings empirisch bestätigt (P1.17-25, P0.10-13). **ABR1 LONG vs SHORT getrennt = entscheidender SUCCESS_CLASS_IDX-Test (P2.38).**
13. **ROM1-WR vs Overall-Bot-WR** — der eigentliche Erfolgs-KPI des Orchestrators; + Verteilung `|sl-entry1|/entry1` über ROM1-Rows → P2.27.
14. **Per-Strategie Realized-WR/PnL** aus `closed_trades_master`/`closed_ai_signals`, **fee-adjustiert** (P3.6), gegen die publizierten Backtest-Zahlen (UFI1 54.2%/+278R → P0.11; SMC-Thresholds → P0.10). `targets_hit`-Verteilung: wie oft Close jenseits TP3/TP5 (P2.31).
15. **Datenqualität:** Gap-Census pro TF (generate_series-Anti-Join) → R1/P2.13; `ma_200=0`-Rows in jungen Coins → P1.13; `COUNT(DISTINCT poc)` über alte Rows → P1.12; Boundary-Overwrite: ~100 recent closed candles DB vs Binance-REST → P1.11.
16. **Korrupte/riskante Trades:** `active_trades_master WHERE (direction='LONG' AND target1<=entry) OR target1=0` → P0.7; `sl<=0 OR NULL` → P2.9; DELISTED-Closes auf non-USDT → P2.17.
17. **Gap-Filler-Beweis:** Housekeeping-Logs enthalten nie "Kerzen gefüllt" → P0.9.
18. **Vokabular-Abgleich:** `DISTINCT model_name`/`strategy` vs Master-Modell-Dummies (erwartet: nahezu 0 Overlap) → P0.13.
19. **Whale-Coverage:** distinkte Symbole in `whale_data/*.json` vs coins.json → P1.42.
20. **Fehlende Tabellen:** wie viele coins.json-Symbole ohne `_1h/_4h(_indicators)` → P1.31 (≥8 = stille Trainer-Truncation); METALS-Tabellen (`XAUUSDT` etc.) existieren überhaupt? → P2.45.

**Regression-Guard scharf schalten** (Step 2): `guard.py extract` + `refresh` gegen die Live-DB, Fixtures/Golden committen → P2.50.

---

### Rohberichte (Details je Subsystem)
Vollständige Einzel-Findings mit Evidence/Snippets liegen unter
`audit_reports/01_core_infra.md … 12_cross_cutting.md`
(01 Core, 02 Datenpipeline, 03 Telegram/Monitore, 04 Orchestrator/Regime, 05 Classic-Strats, 06 AI 9/11/12/13, 07 AI 14/15/18, 08 SMC-Bots, 09 Market-Intelligence, 10 Dashboard/Tools, 11 ML-Trainer/Backtests, 12 Cross-Cutting).
Dazu: 13 ML-Trainer-Audit (`Documents\_X`), 14 Live-Performance aus der DB, 15 Strategie-Vorschläge, 16 Strategie-/Modell-Konzeptbewertung (Noten A–F + Portfolio-Empfehlungen), STEP2_DB_VERIFICATION (Live-Beweise).
