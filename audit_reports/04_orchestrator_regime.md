# Agent 4: Orchestrator + Regime (28, 26, 27, core/regime_logic, docs/REGIME_ORCHESTRATOR.md)

### [CRITICAL] [bug] Whitelist-Gate läuft für MIS1-* und Classic-Bots komplett ins Leere (Bot-Name-Mismatch, Default-Open)
- 28:134-148, 240-251, 556; 27:322; core/bot_naming.py:36-74. Analyzer normalisiert mit pretty_name ('MIS1-8h', 'FastInOut'); Orchestrator wendet pretty_name NIE an (import fehlt) → Lookup 'MIS1-8H'/'Fast In And Out' case-sensitiv → keine Row → (True, "no_whitelist_entry") → Signal IMMER durchgereicht. Auch Fallback + check_regime_change_and_close inert. Test zementiert Roh-Namen.
- Fix: pretty_name(bot_name) nach identify_bot + beim Insert; Default-Open-Rate monitoren.
- DB-phase: reason-Verteilung suppressed_signals; bot_name in open_trades vs whitelist.

### [CRITICAL] [bug] Orchestrator konsumiert eigene ROM1-Posts aus der Outbox; Dedup hängt allein am Cooldown → Doppel-Trade-Pfad
- 28:386-421, 524-537, 597-619. ROM1-Message enthält "Triggered by: MIS1-8H" → matcht BOT_IDENTIFICATION_PATTERNS; kein Channel-Filter im Scan-SELECT → Self-Echo durch die ganze Pipeline; nur 4h-Cooldown schützt, der erst NACH send_telegram committed wird. Crash zwischen Post und Cooldown → Restart → zweiter geleveragter Trade. Normalbetrieb: Müll-Rows in suppressed_signals.
- Fix: channel_id != REGIME_TRADING_CHANNEL_ID im SELECT; ROM1-Marker als Hard-Reject; Cooldown/Tracking VOR send committen.

### [HIGH] [bug] sent=FALSE-Filter racet gegen Telegram-Dispatcher → Signale still nie gegated
- 28:524-537 vs 4:183-194, 263, 338. Dispatcher markiert sent=TRUE binnen ~0.1-0.5s; Signal zwischen zwei Orchestrator-Pässen inserted+sent → fällt aus SELECT → nie bewertet, kein Log. _last_seen_outbox_id macht sent-Filter überflüssig.
- Fix: sent/failed-Bedingungen entfernen; Neuheit über id-Cursor + Fenster.

### [HIGH] [data-integrity] Forward-Pipeline nicht atomar; Batch-Cursor erst am Pass-Ende → Fired-but-untracked / Batch-Replay
- 28:597-627. 4 Schritte mit eigenen Commits (send → rom1 → open_trade → cooldown); Crash nach send → Trade bei Cornix ohne ai_signals + ohne open_trades (Monitor/Regime-Close sehen ihn nie). Exception bei Row 5/10 → Rows 1-4 (gepostet) im nächsten Pass erneut.
- Fix: DB-Writes in EINER Txn zuerst, Outbox-Insert zuletzt; Cursor pro Row; Exceptions pro Row.

### [HIGH] [data-integrity] sync_closed_trades matcht fremde Trades (kein model-Filter, kein ORDER BY, Fenster 720h) → falsche Outcomes, vorzeitiger Verlust des Opposite-Schutzes
- 28:879-925. Beliebige coin+direction-Row eines ANDEREN Bots binnen 30 Tagen "schließt" den ROM1-Trade → (a) ROM1-Statistik Zufall, (b) Opposite-Check weg → Hedge/Doppel-Exposure, (c) Regime-Close überspringt.
- Fix: nur closed_ai_signals WHERE model='ROM1', Fenster ±60s gegen open_time, ORDER BY.

### [HIGH] [data-integrity] Regime-Close löscht ALLE offenen Trades aller Bots auf coin+direction — nicht nur Orchestrator-Trades
- 28:672-817. ai_signals/active_trades_master nur coin+direction gefiltert → Paper-Trades fremder Bots als CLOSED_REGIME_CHANGE zensiert, korreliert mit Regime-Wechseln (Verlustphasen) → Whitelist-Winrates nach oben gebiast (das Geld-Gate!).
- Fix: nur model='ROM1' bzw. via original_outbox_id.
- DB-phase: COUNT by model/strategy WHERE status='CLOSED_REGIME_CHANGE'.

### [HIGH] [spec-drift] Doku beschreibt reinen Signal-Router — Code generiert eigenständige Trades mit eigenen Entries/SL/Targets
- docs:18,53-59,131-137 vs 28:288-421. ROM1 verwirft Original-Parameter, berechnet eigene aus 5m-Close + HVN/SR → Gating-Statistik (erhoben mit Original-Parametern) gilt nicht für ROM1-Ausführung. Undokumentiert: 60s-Fenster, 4h-Cooldown, Opposite-Block, Force-Close, Default-Allow.
- Fix: Doku auf v6; "Gating-Statistik ≠ Ausführungs-Statistik" als Risiko dokumentieren.

### [MEDIUM] [bug] TZ-Mix: update_cooldown NOW() (Session-TZ) in naive Spalte, Check liest als UTC; Outbox-Fenster naive-UTC vs timestamptz
- market_utils:123-135 vs 98-120; 28:521-536; 4:60. DB-TZ Vienna → 4h-Cooldown effektiv 6h; 60s-Fenster wird 2h-Fenster (Restart-Replays reichen weiter zurück).
- Fix: NOW() AT TIME ZONE 'UTC' bzw. aware Params.

### [MEDIUM] [bug] Training/Serving-Skew: Analyzer attribuiert auf RAW regime_history, Gating läuft auf debounced regime_current
- 27:227-236, 273-282 vs regime_logic:263-422. Um Übergänge (meiste Signale) systematisch falsche Regime-Zuordnung. Backfill-Look-ahead: letzte 15m-Kerze mit open_time<=as_of noch nicht geschlossen.
- Fix: Attribution auf debounced Zustand (effective_regime-Spalte); Backfill open_time+15min<=as_of.

### [MEDIUM] [robustness] Detector-"Unreliable"-Heuristik zählt RAW-Flaps → System kann dauerhaft im Overall-Fallback hängen
- 28:177-191. COUNT(DISTINCT regime) über rohe History; TRANSITION als Auffang-Klasse → ≥3 distinct leicht → 4D-Gating durch groben Overall-WR-Filter ersetzt, potenziell meiste Zeit. Fallback-Rate wird (entgegen Doku) nicht im Status-Post ausgewiesen.
- Fix: distinct auf debounced Wechsel; Fallback-Rate als Metrik.

### [MEDIUM] [robustness] Regime-Wechsel während Orchestrator-Downtime nie nachgeholt (In-Memory _last_known_regime)
- 28:76-77, 949-952. Baseline-Init returned ohne Re-Evaluation offener Trades.
- Fix: beim Start alle OPEN-Trades gegen aktuelle Whitelist prüfen; State persistieren vs regime_current.since.

### [MEDIUM] [data-integrity] Stale bot_regime_whitelist-Rows nie bereinigt — Cleanup nur auf Performance-Tabelle
- 27:747-793 (nur perf), 625-643 (Whitelist UPSERT ohne DELETE). Alt-Keys (MIS1-8H uppercase) mit eingefrorenen Entscheidungen; Orchestrator fragt mit genau diesen Roh-Namen an → gated auf monatealten Daten.
- Fix: Cleanup auf whitelist ausdehnen; computed_at-Staleness-Gate (>26h → wie no_whitelist_entry + Warnung).

### [MEDIUM] [bug] Kein Same-Direction-Open-Check: nach 4h Cooldown stapelt ROM1 Positionen auf denselben Coin+Richtung
- 28:272-284, 569-577. Exposure pro Coin faktisch unbegrenzt.
- Fix: is_same_direction_open → suppress (oder Stacking dokumentieren+limitieren).

### [MEDIUM] [bug] ROM1-SL ohne Distanz-Cap — nächste S/R-Zone kann 30-50% weg sein, bei 20x jenseits Liquidation
- 28:355-366 vs trade_utils:172-211 (calculate_smart_targets HAT Caps 15%/10%; ROM1-Variante nicht). Fallback-SL ~7.6%.
- Fix: gleiche Hard-Caps; Risiko dokumentieren.
- DB-phase: Verteilung |sl-entry|/entry über ROM1-Rows.

### [MEDIUM] [security] String-interpolierte Tabellennamen aus geparstem Message-Text ("{coin}_5m")
- 28:312, 660; trade_utils:264. coin aus re.search(r"Signal for\s+(\S+)") — \S+ erlaubt beliebige Zeichen; Outbox ist shared Table → Second-Order-Injection-Pfad mit dbfiller-Rechten.
- Fix: Symbol-Whitelist gegen coins.json / Regex vor Verwendung.

### [MEDIUM] [robustness] 60s-Fenster + start_delay=175 → alle Signale rund um jeden Restart kommentarlos verworfen
- 28:35, 521-536. Jeder Restart wirft ≥3 min Signalstrom weg, ohne Log/Metrik.
- Fix: Fenster 5-10 min + stale_signal-Logging in suppressed_signals; dokumentieren.

### [LOW] [spec-drift] Doku-Details falsch: regime_current-Init (Cold-Start-Insert beim ERSTEN Check), ↑/↓-Marker nie implementiert (nur Legende), Fallback-Rate im Status-Post fehlt; Anzeige-Schwelle n<20 vs Entscheidung n<30.
### [LOW] [performance] ensure_regime_schema bei JEDEM 5-min-Check (10+ CREATE IF NOT EXISTS); regime_history/suppressed_signals/perf ohne Retention.
### [LOW] [code-quality] Duplizierte tote Konstanten in 26 (Tuning dort wirkungslos — Doku verweist genau dorthin!); regime_current.confidence zeigt Raw- statt effektive Confidence.
### [LOW] [robustness] identify_bot-Lücken: MAYANK/SMC nie identifizierbar (bot_unidentified forever); UFI1 nur fragiler Footer-Regex; IGNORECASE erzeugt Case-Varianten (verschärft Finding 1). Fix: standardisiertes module_tag-Feld in allen Cornix-Messages.
### [LOW] [data-integrity] Kleinkram: entry_price REAL; Sync-Deadline 30d → ewig OPEN; Classic-Force-Close-Status beim Sync ignoriert (CLOSED_REGIME_CHANGE → als TP/SL klassifiziert statt neutral); Daily-Post-Default "TREND_UP" bei leerem regime_current.

## Cross-cutting observations
1. Das Sicherheitsnetz ist de facto der 4h-Cooldown (mit TZ-Bug, als letzter Schritt committed). EIN robustes Primitive entschärft fast alle HIGHs: atomarer Claim auf der Outbox-Row (UPDATE ... SET orchestrator_processed=TRUE ... RETURNING id) VOR jeder Aktion — persistent, crash-sicher, mehrfachstart-sicher.
2. Default-Open als Policy: no_whitelist_entry/fallback/insufficient_data (n<30 pro 4D-Zelle bei 30 Zellen) → Orchestrator heute näher an "Repost-Bot mit Cooldown" als an Regime-Filter. Forwards pro wl_reason als Metrik.
3. Statistik-Hygiene: drei systematische Aufwärts-Biases der WR-Pipeline: Open-Trade-Zensierung, Regime-Change-Closes als neutral ENTFERNT statt realisiert, Fremd-Trade-Zensierung. Für ein Gate mit Zehntel-pp-Vergleichen relevant.
4. Positiv: Debounce korrekt; PnL-Vorzeichen überall richtig; _classify_outcome defensiv; keine Div-by-Zero; parametrisierte INSERTs; idempotente UPSERTs.

## Questions for live-DB phase
1. SHOW timezone + NOW() vs NOW() AT TIME ZONE 'UTC'.
2. DISTINCT bot_name in whitelist vs open_trades vs suppressed (Beweis Mismatch + stale Keys, MIN/MAX computed_at).
3. wl_reason-Verteilung 30 Tage: wieviel % Forwards über echte 4D-Entscheidung?
4. Fallback-Quote simulieren über regime_history; TRANSITION-Anteil.
5. suppressed_signals mit original_outbox_id → CH_REGIME_TRADING (Self-Echo-Beweis); bot_unidentified-Cluster.
6. open_trades: OPEN >30d; coin+direction-Duplikate; lifecycle_sync-Rows gegen echte Matches prüfen.
7. Enthalten *_15m/*_5m die laufende Kerze?
8. ROM1-Risiko-Ist: SL-Distanzen; ROM1-WR vs Overall (der eigentliche KPI).
9. Watchdog-Logs: je zwei Orchestrator-Prozesse gleichzeitig?
