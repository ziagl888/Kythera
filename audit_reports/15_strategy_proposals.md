# 15 — Vorschlagsliste: neue Strategien & Modelle (Step 5, Konzept)

**Stand:** 2026-07-03 · **Grundlage:** Realisierte Ergebnisse (Report 14), Kalibrierungsmessungen (Step 2), Trainer-Audit (Report 13) sowie vier gezielte Hypothesen-Tests gegen die Live-DB (Konfluenz, Regime-Konditionierung, AIM1-Fade, FIFO-Tail-Anatomie; Skript `step5_hypotheses.py`).

**Empirische Bausteine, auf denen die Vorschläge stehen:**

| # | Befund (dedupliziert, aktive Ära) | Zahl |
|---|---|---|
| E1 | Richtungs-Asymmetrien sind groß und stabil | EPD1 SHORT 76,5% vs LONG 50,2% WR; RUB1 SHORT 63,9% vs LONG 48,7%; BR1H LONG 65,5% vs SHORT 49,5% |
| E2 | Regime konditioniert Richtung (orchestrator `regime_at_open`, n=719) | CHOP: SHORT 66,4%/+1,98% vs LONG 59,4%/**−3,69%**; HIGH_VOLA: LONG nur 48,4%; TRANSITION: beide ~63-64% positiv |
| E3 | Konfluenz: genau 2 unabhängige Modelle auf Coin+Richtung binnen 4h | LONG: 65,7% WR/+1,40% (vs 61,4% solo); **4+ Modelle = Kontra-Signal: 51% WR, −1,0%** (Crowding/Vol-Event) |
| E4 | Echt kalibrierte Modelle existieren | TD_1H: 78,5% WR @conf>0.9; SRA1, MIS1-8H, QM_1H positiv kalibriert |
| E5 | AIM1 ist systematisch invers | conf 0.9–0.95: **8,3% WR, −9,53%/Trade (n=19.295)**; aber conf>0.95 kippt auf 85% WR (n=267) |
| E6 | FIFO-Verluste sind breit, nicht tail-lastig | Loss-Cap −3% verbessert ø nur um 0,02pp → Problem ist Selektion, nicht Ausreißer |
| E7 | Profite leben in den Tails | Median-PnL ≈ 0 fast überall; Summen aus p95 (MIS1-72H, RUB1) |
| E8 | 44,5% der Zeit ist TRANSITION-Regime | Orchestrator läuft dann im groben Fallback |

**Vorbehalte:** Alles monitor-generiert (P1.2/P2.7/P1.9), ungehebelt, Regime-Daten erst seit ~April, H2/H3-Zellen teils kleine n. Jeder Vorschlag muss durch den First-Touch-Simulator (P0.10-Fix) und eine Shadow-Phase (`ml_predictions_master`, posted=false — Infrastruktur existiert!) bevor echtes Geld dranhängt.

---

## Voraussetzungen (Fundament — ohne das ist jede neue Strategie auf Sand gebaut)

- **V1:** R1-Fix (Closed-Candle-Vertrag) — sonst erben neue Modelle den Look-ahead.
- **V2:** Dedup-Index auf `closed_ai_signals` + Purge (Report 14 A1) — sonst sind Trainingslabels verseucht.
- **V3:** Gemeinsamer **Walk-Forward-First-Touch-Simulator** (= P0.10/X-R1-Fix): eine Bibliothek, die für jedes Setup die tatsächlich gepostete Order-Geometrie (Entry1/Entry2, SL, Targets, Trailing) bar-für-bar abspielt. Er ist gleichzeitig Label-Quelle für neue Modelle UND Backtest-Engine für neue Strategien.
- **V4:** TZ-Fix (R3), damit Zeitfenster-Features stimmen.

---

## Tier 1 — Meta-Strategien ohne neues ML (Tage, nutzen vorhandene Signale)

### S1 — „Direction-Gated Portfolio": bestehende Bots auf ihre profitable Seite beschneiden
- **Evidenz:** E1. **Konzept:** Konfigurierbare Direction-Gates im Orchestrator/je Bot: EPD1 nur SHORT, RUB1 nur SHORT, BR1H nur LONG, 5-Percent-LONG-Seite prüfen (n klein). **Erwartung:** hebt die Fleet-WR um mehrere Punkte, kostet nichts außer Signalmenge. **Risiko:** Asymmetrie kann regimebedingt sein → Gates monatlich gegen rollierende Fenster re-validieren. **Umsetzung:** Regelwerk + Konfig-Tabelle, 1–2 Tage.

### S2 — „Regime-Richtungs-Matrix" (Orchestrator 2.0)
- **Evidenz:** E2. **Konzept:** Nach dem P0.4-Whitelist-Fix eine zweite Gate-Ebene: `CHOP → nur SHORT` (Longs dort −3,7%/Trade!), `HIGH_VOLA → keine LONGs (außer explizit whitelisted)`, `TRANSITION → beide Seiten zulassen` (entgegen der Intuition die beste Zone, E8 macht sie zur größten). Matrix aus `regime × alt_context × direction` datengetrieben aus `orchestrator_open_trades`-Outcomes gepflegt, mit Mindest-n und Konfidenzintervall statt Punktschätzer. **Erwartung:** ersetzt die kaputte per-Bot-Whitelist-Logik durch ein robusteres, gröberes Gate mit mehr Daten pro Zelle. **Risiko:** n=719 ist noch dünn → 4–6 Wochen Shadow-Parallellauf, Zellen erst scharf schalten ab n≥100.

### S3 — „Confluence-2-Booster + Crowding-Abstain"
- **Evidenz:** E3. **Konzept:** Signal-Router zählt distinct Modelle je (Coin, Richtung, 4h-Fenster): bei **genau 2–3** → Positionsgröße erhöhen/priorisieren; bei **≥4** → unterdrücken (oder als Kontra-Beobachtung loggen). Das ist ein reiner Zähler im Orchestrator — kein Modell nötig. **Erwartung:** LONG-Konfluenz-2 lief +1,40%/Trade bei 65,7% WR. **Risiko:** Fenster/Schwelle wurden auf denselben Daten gefunden → out-of-time auf Mai–Jul separat validieren (Split verfügbar).

### S4 — „Calibration-Sized Positions"
- **Evidenz:** E4. **Konzept:** Für die nachweislich kalibrierten Modelle (TD_1H, SRA1, MIS1-8H, QM_1H) Positionsgröße ∝ (kalibrierte Prob − Break-even-Prob) (Fractional-Kelly, hart gecappt); unkalibrierte Modelle bekommen Einheitsgröße. Kalibrierung per Isotonic auf rollierendem Out-of-Time-Fenster, monatlich neu. **Erwartung:** verlagert Kapital dahin, wo Confidence real Information ist (TD_1H@>0.9 = 78,5% WR). **Risiko:** Kalibrierung driftet → Auto-Degradation auf Einheitsgröße, wenn Reliability-Kurve bricht.

### S5 — „AIM1-Fade" (NUR Shadow-Experiment)
- **Evidenz:** E5. **Konzept:** AIM1-Signale mit conf 0.85–0.95 invertieren (LONG-Signal → SHORT-Kandidat). Auf Papier wäre das ~+9,5%/Trade vor Kosten über 19k Beobachtungen gewesen — eine der stärksten „Edges" im Datensatz. **ABER:** Die Inversion ist ein Out-of-Distribution-Artefakt (Report 13) — sie kann mit jedem Datendrift verschwinden oder kippen (conf>0.95 gewinnt bereits 85%!). **Deshalb:** ausschließlich als Shadow-Strategie in `ml_predictions_master` mitschreiben, 8+ Wochen beobachten, niemals blind live. Realistischer Nutzen: AIM1-hoch-conf als **Veto-Feature** für andere Bots (wenn AIM1 >0.85 sagt, Finger weg von dieser Richtung).

---

## Tier 2 — Neue Modelle auf vorhandener Datenbasis (Wochen, brauchen V1–V3)

### S6 — „Pump-Exhaustion-Short" (EPD1-Nachfolger, Short-only)
- **Evidenz:** E1 (EPD1 SHORT 76,5%/+3,3%), Report 13 B-1 (Gate-Bug). **Konzept:** Dediziertes Short-Modell auf Pump-Erschöpfung: Trainings-Samples NUR bei `vol_ratio ≥ 5` (Gate live gespiegelt!), Microstructure-Features aus `ticker_10s` (buy_pressure-Abfall, Volumen-Decay, Spike-Alter), Label = First-Touch der echten SR-basierten Short-Geometrie. Long-Seite komplett weglassen. **Warum aussichtsreich:** Der bestehende EPD1 verdient trotz kaputter Befragung; ein sauber ge-gatetes Short-only-Modell auf denselben Daten ist die naheliegendste Verbesserung mit vorhandener Infrastruktur (`pump_dump_events`, ticker-Puffer).

### S7 — „AIM2": das Meta-Modell richtig gebaut
- **Evidenz:** Report 13 (alle Ursachen bekannt), E4. **Konzept:** Neutraining des Master-Meta-Modells mit: aktuellem Vokabular aus DB-DISTINCT (nicht hardcoded), floor−1-Join, Label = First-Touch der echten Geometrie, **Regime-Features aus `regime_history`** (gab es 2025 noch nicht — heute der offensichtlichste fehlende Prädiktor), Quell-Modell-Kalibrierungsscore als Feature, zeitlicher 3-Wege-Split, Isotonic-Kalibrierung, reindex-Parity-Guard. **Rolle:** nicht eigenständiger Trader, sondern **Ranker/Sizer** über alle Quellsignale (ersetzt S4-Heuristik langfristig).

### S8 — „Funding-Extreme Mean-Reversion"
- **Evidenz:** Funding-Daten liegen seit Februar lückenlos (`funding_data/funding_history_*.json`, Logger läuft); P2.40 zeigte, dass die aktuelle 75%-Schwelle im Normalzustand feuert — d.h. das Signal ist ungenutzt. **Konzept:** Cross-sectional: Coins im obersten Funding-Perzentil (≥95., überhitzte Longs) SHORT, unterstes Perzentil LONG, Halten bis Funding-Normalisierung oder Time-Stop; optional nur bei passendem Regime (CHOP/TRANSITION). **Warum aussichtsreich:** klassische, ökonomisch begründete Edge (Carry + Crowding-Unwind), komplett neue Signalquelle orthogonal zur bestehenden Flotte. **Erst:** Backtest auf den 4 Monaten Funding-Historie via V3-Simulator.

### S9 — „Cross-Sectional Long/Short-Basket" (Portfolio statt Einzelsignale)
- **Evidenz:** 529 Coins × volle OHLCV+Indikator-Historie in der DB — von keiner bestehenden Strategie cross-sectional genutzt; E7 (Tail-Profite) spricht für Portfolio-Ansätze. **Konzept:** Täglich/4h-Rebalancing: Ranking aller Coins nach Momentum- (z.B. 7d-Return, ADX) bzw. Reversal-Score (Abstand zu MA, RSI-Extrem), Top-Dezil LONG / Bottom-Dezil SHORT, **delta-neutral** → Marktrichtung egal, verdient an Dispersion. **Besonderheit:** braucht einen Portfolio-Executor (n Positionen gleichzeitig, Rebalancing) statt des Signal-für-Signal-Cornix-Flows — größerer Umbau, aber die Datenlage dafür ist bereits perfekt.

### S10 — „Transition-Resolution-Modell"
- **Evidenz:** E8 (44,5% TRANSITION), E2 (TRANSITION-Trades sind gut!). **Konzept:** Kleines Modell, das NUR im TRANSITION-Regime läuft und die Auflösungsrichtung (→BULL_TREND/BEAR_TREND/CHOP) aus `regime_history`-Rohfeatures (btc_return_1h/4h, atr, btcdom, confidence-Verläufe) vorhersagt; Output gated die Richtung aller anderen Bots während der Transition. **Warum aussichtsreich:** Es adressiert direkt die größte Schwäche des Regime-Systems (P2.23: Fallback dominiert) in dem Zeitfenster, das fast die Hälfte der Uhrzeit ausmacht — und die Zieldaten (nächstes stabiles Regime) sind aus `regime_history` trivial labelbar.

### S11 — „FIFO-Filter-Modell" (Selektion statt neuer Signale)
- **Evidenz:** E6 — FIFO hat 111k gelabelte Trades, Median +1,25%, ø −0,13%; das Problem ist Selektion, nicht Tails. **Konzept:** Meta-Klassifier, der VOR dem Posten eines Fast-In-And-Out-Signals aus Entry-Zeitpunkt-Features (Regime, Richtung, Konfluenz-Zähler, RSI/ATR/Volumen-Kontext, Coin-Liquiditätsklasse) die Gewinnwahrscheinlichkeit schätzt; nur Top-X% durchlassen. **Warum aussichtsreich:** größter gelabelter Datensatz im Haus, klar definierte Frage, und selbst +0,3pp ø-Verbesserung dreht die Strategie von −25.8k auf positiv. Gleiches Muster danach auf Volume Indicator (51k Trades) übertragbar.

---

## Tier 3 — Nach Infrastruktur-Fixes

- **S12 — Whale-Flow-Confirmation:** Nach P1.42-Fix (Sharding, Logger tot seit 18.4.): Whale-Netflow als Bestätigungs-Feature für S6/S11. Erst Datenqualität, dann Modell.
- **S13 — Exit-Redesign „Tail-Harvesting":** E7: Für Tail-Träger (MIS1-72H, RUB1) Runner-Exits testen (TP1 klein zur Kostendeckung, Rest mit Chandelier-Trail) — im V3-Simulator gegen die Ist-Exits, bevor irgendein Live-Exit geändert wird. Für Classic (FIFO/5-Percent) umgekehrt: TP/SL-Geometrie so setzen, dass ø nach Fees positiv wird, sonst abschalten.

---

## Empfohlene Reihenfolge

1. **V1–V3** (Fundament; V3-Simulator ist der Schlüssel zu allem).
2. **S1 + S3** (reine Konfig/Router-Regeln, sofort validierbar out-of-time) → erste risikoarme Verbesserung.
3. **S2** im Shadow-Betrieb starten (sammelt gleichzeitig die Daten, die die Matrix robust machen).
4. **S11** (FIFO-Filter) als erstes neues Modell — beste Daten/Aufwand-Relation.
5. **S6** (Pump-Exhaustion-Short) und **S8** (Funding) parallel als neue Alpha-Quellen.
6. **S7** (AIM2) sobald V3 steht; **S9/S10** danach; **S5** läuft die ganze Zeit nur als Shadow-Logger mit.

Jeder Kandidat durchläuft: Simulator-Backtest (V3, walk-forward, fees) → 4–8 Wochen Shadow (`ml_predictions_master`) → Kalibrierungs-/Reliability-Check → kleines Live-Sizing → Skalierung. Abbruchkriterien vorab definieren (z.B. Shadow-WR-CI unterschreitet Break-even).
