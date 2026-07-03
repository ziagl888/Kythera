# 16 — Regime-Orchestrator: Gesamtanalyse & Verbesserungsvorschläge (Step 6)

**Stand:** 2026-07-03 · Konsolidiert Code-Audit (Report 04), Live-DB-Beweise (Step 2), Performance (Report 14), Hypothesen (Report 15) + neue empirische Auswertung des Gesamtsystems (`step6_orchestrator.py`).

## 1. Architektur (Ist)

```
26_regime_detector ──5min──▶ regime_history (raw) ──debounce──▶ regime_current
                                                                      │
27_bot_regime_analyzer ──▶ bot_regime_performance ──▶ bot_regime_whitelist
   (Outcome-Attribution)      (Bot×Regime×Alt×Dir           │
                               ×3 Fenster, 4.056 Zellen)    ▼
28_signal_orchestrator: telegram_outbox scannen ▶ identify_bot ▶ Whitelist-Gate
   ▶ Forward als ROM1 (eigene Geometrie!) ▶ orchestrator_open_trades
   ▶ bei Regime-Wechsel: Auto-Close ALLER offenen Trades auf Coin+Richtung
```

## 2. Empirische Bestandsaufnahme — funktioniert das Kern-Feature?

**Was nachweislich funktioniert:**
- **ROM1 liefert Mehrwert:** 69,2% WR vs 61,1% Fleet (+8pp), +0,92%/Trade, +2.184 netto (n=2.677) — trotz aller Bugs.
- Lifecycle ist dicht: 0 OPEN-Trades älter als 7 Tage; Pipeline läuft stabil durch (4.339 Closes seit 18.04.).
- Das Gate greift real: Gate-Rate 44,7% (Apr) → 63,5% (Jun); Cooldown- und Opposite-Direction-Guards feuern.

**Was nachweislich kaputt oder hohl ist:**

| # | Befund | Zahl |
|---|---|---|
| B1 | **Whitelist ist zu 89% Default-Open.** Frische Rows: 747× `insufficient_data` (=durchwinken), nur 41× `wr_above_overall` + 52× `wr_below_overall` datenbasiert | 11% echte Entscheidungen |
| B2 | **Datengrundlage zu dünn für die 4D-Matrix:** 4.056 Zellen, Median **7 Trades/Zelle**, 68% unter 30 | Matrix zu fein |
| B3 | **Detector kennt praktisch nur 3 Regimes:** in 5,5 Monaten 5× TREND_DOWN- und 2× TREND_UP-Episoden (alle <1h!); Verteilung TRANSITION 44,5% / HIGH_VOLA 29,7% / CHOP 25,8% | TREND-Klassen tot |
| B4 | **52% aller Raw-Episoden sind <1h-Flaps** (654/1.257); Median-Dauer CHOP/TRANSITION 0,9h; Confidence median 0,54, p10 0,40 | Detector flattert |
| B5 | **Gate entscheidet auf 2,5 Monate alten Daten** für MIS-Familie + Channel-Bots (P0.4-Raw-Namen, eingefroren 19.04.) — die Junigate-Rate von 63,5% basiert dort auf April-Statistik | bewiesen (Step 2) |
| B6 | **Auto-Close schneidet blind:** 3.653 REGIME_CHANGE-Closes, median PnL **0,00%**, 49,3% in Gewinn geschnitten; 35% aller ROM1-Trades (1.411/4.339) enden per Regime-Close statt TP/SL → Churn + Fees + zensierte Statistik (P1.9: trifft ALLE Bots, nicht nur ROM1) | AIM1 wird im Schnitt bei **+9,5%** gekappt |
| B7 | **Self-Echo existiert:** 109 Suppressions stammen aus dem eigenen Regime-Channel (86 davon als `bot_unidentified`) — nur Cooldown verhindert bisher Schlimmeres (P0.3) | bewiesen |
| B8 | **Forwards werden nicht begründet geloggt** (kein `wl_reason` in `orchestrator_open_trades`) → man kann nicht messen, welcher Gate-Pfad Geld verdient | Messlücke |
| B9 | **Zirkularität:** Der Analyzer lernt aus Outcomes, die der Orchestrator selbst zensiert (B6/P1.9) — Regime-Wechsel-Verluste werden als "neutral" aussortiert → Whitelist-WRs systematisch geschönt | strukturell |
| B10 | `bot_unidentified` = 841 Suppressions gesamt (drittgrößter Grund) — identify_bot-Patterns decken den Signalstrom nicht ab | Pattern-Lücken |

**Einordnung:** Das System verdient Geld **trotz**, nicht **wegen** seiner Whitelist: Bei 89% Default-Open + stale Raw-Rows wirkt de facto vor allem (a) der 4h-Cooldown, (b) der Opposite-Direction-Guard und (c) die grobe Fallback-Heuristik. Der eigentliche 4D-Kern (Bot×Regime-Selektion) ist statistisch unterbesetzt und zum Teil eingefroren.

## 3. Verbesserungsvorschläge

### Stufe 1 — Reparieren (bekannte Bugs, Tage; Referenzen = AUDIT_TODO)
1. **P0.4-Fix:** `pretty_name()` direkt nach `identify_bot()` + Purge der April-Raw-Rows + `computed_at`-Staleness-Gate (>48h alt ⇒ Zelle gilt als `insufficient_data`), Alarm auf Default-Open-Rate.
2. **P0.3-Fix:** `channel_id != REGIME_TRADING_CHANNEL_ID` im Scan-SELECT + ROM1-Hard-Reject; Cooldown/Tracking VOR dem Send committen (P1.7: Txn zuerst, Outbox-Insert zuletzt, Cursor pro Row).
3. **P1.9-Fix:** Auto-Close nur `model='ROM1'`; `sync_closed_trades` mit Model-Filter + ±60s-Match (P1.8); `sent=FALSE`-Race durch id-Cursor ersetzen (P1.6).
4. **B10:** identify_bot-Patterns gegen die 841 `bot_unidentified` nachziehen (Kandidaten stehen im Suppressed-Log).
5. **B8:** `wl_reason`-Spalte an `orchestrator_open_trades` — ab dann ist jeder Forward begründet und auswertbar.

### Stufe 2 — Statistik ehrlich machen (Wochen)
6. **4D-Matrix durch hierarchisches Shrinkage ersetzen.** Median 7 Trades/Zelle trägt keine Entscheidung. Vorschlag: Empirical-Bayes — Zell-WR wird zum übergeordneten Mittel (Bot-gesamt → Regime×Richtung → Fleet) geschrumpft, Gewicht ∝ n. Effekt: keine `insufficient_data`-Binärkrücke mehr, jede Zelle liefert eine benutzbare, konservative Schätzung; Gate-Schwelle auf der Shrinkage-WR mit Konfidenzintervall (z.B. untere Wilson-Grenze > Break-even).
7. **Zensur-Korrektur (B9):** Regime-geschlossene Trades nicht verwerfen, sondern mit PnL-zum-Close-Zeitpunkt in die Statistik nehmen (Median 0% → neutraler Effekt, aber kein Selektions-Bias mehr).
8. **Suppressed-Counterfactual-Scorer (neu, hoher Hebel):** Ein Job, der für jede Suppression (Outbox-Row ist verlinkt!) das hypothetische Outcome per First-Touch-Simulator nachrechnet und an `orchestrator_suppressed_signals` schreibt. Damit wird der Gate-Wert **laufend messbar**: "Suppressions ersparten X% / kosteten Y%". Heute ist der Nutzen des Kern-Features schlicht unbekannt — das beendet jede Diskussion mit Daten.

### Stufe 3 — Detector & Gating-Logik weiterentwickeln
9. **Detector-Revision (B3/B4):** (a) TREND-Klassen brauchen eigene Features (EMA-Slope-Persistenz, ADX, Higher-Highs-Zählung) — aktuell erreichen sie die Debounce-Schwelle nie; (b) Flap-Rate 52% ⇒ Hysterese verlängern oder Bestätigungszähler adaptiv (kurz in HIGH_VOLA, lang in CHOP); (c) Confidence kalibrieren (p10=0,40 heißt: oft rät er) und unter Schwelle explizit `UNKNOWN` melden statt CHOP zu raten.
10. **TRANSITION entdecken statt erleiden:** 44,5% der Zeit ist "Übergang" — entweder Detector feiner auflösen oder das **Transition-Resolution-Modell (S10, Report 15)** bauen, das die Auflösungsrichtung vorhersagt. Die Step-5-Daten zeigen: TRANSITION-Trades sind gut (63-64% WR) — das Regime ist handelbar, der Fallback behandelt es nur zu grob.
11. **Regime-Richtungs-Matrix als zweite Gate-Ebene (S2):** grob (Regime×Richtung, 6-15 Zellen mit hunderten Trades) statt/vor der feinen Bot-Matrix: CHOP→nur SHORT (Longs −3,69%/Trade), HIGH_VOLA→LONGs droppen. Robust, sofort datentragfähig.
12. **Auto-Close-Politik differenzieren (B6):** Statt Market-Close aller Positionen beim Wechsel: (a) Gewinner: SL auf Break-even/Trail statt Close (49% werden derzeit im Gewinn gekappt, AIM1 bei +9,5%!); (b) Verlierer: schließen wie bisher; (c) Counterfactual-Scorer (Nr. 8) misst, ob die Policy Geld spart.
13. **ROM1-Geometrie:** Original-Signal-Geometrie durchreichen (oder mind. SL-Distanz-Cap + `cap_leverage_to_sl`, R4/P2.27) — aktuell misst die Gating-Statistik andere Trades als die Quell-Bots je gepostet haben (P1.10-Spec-Drift dokumentieren).

### Stufe 4 — Betriebsfestigkeit
14. Startup-Reconcile: nach Downtime alle OPEN-Trades gegen aktuelle Whitelist prüfen (P2.24); Detection-Fenster 5-10 min statt 60s + `stale_signal`-Log (P2.28); Regime-Status-Posts mit Fallback-Rate + Default-Open-Rate als Gesundheitsmetrik (P3.10).

## 4. Ziel-Bild (kompakt)

> Ein Detector mit ehrlicher Unsicherheit (UNKNOWN statt Raten, funktionierende TREND-Klassen), ein zweistufiges Gate (grobe Regime×Richtung-Matrix → fein per Shrinkage-Bot-Score), Forwards und Suppressions beide mit Begründung UND Counterfactual-Outcome geloggt, Auto-Close, das Gewinner trailt statt kappt — und ROM1, das die Geometrie der Quell-Signale respektiert. Jede Komponente misst sich selbst; die Whitelist kann nie wieder still einfrieren, weil Staleness und Default-Open-Rate alarmiert werden.

**Priorität:** Stufe 1 komplett (Bugfixes, ~Tage) → Nr. 8 Counterfactual-Scorer (macht alles Weitere messbar) → Nr. 6+11 (Statistik) → Stufe 3 nach Datenlage.
