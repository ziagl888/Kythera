# Dossier: Volume Indicator

> Volume-Profile-Idee mit degenerierter Mechanik und ohne Cooldown — **Note D+** (Report 16) · brutto +4.439, **netto Σ −705** (Fees fressen alles) bei 51.440 Trades. Kernverdikt: kleiner echter Kern (Volumenzonen wirken), rettbar nur mit Umbau — sonst reiner Fee-Generator.

## 1. Steckbrief
- **Modul:** `strategies/strat_volume_indicator.py`, Runner `3_detectors.py`, Monitoring `5_trade_monitor.py`.
- **Signal-Logik:** Preis an einem 90d-High-Volume-Node (HVN) + 3σ-Volumen-Spike in den letzten 5 Tagen bestimmt die Richtung → 30m-Entry, TP1 +2,5%. Problem: ein bis zu 5 Tage alter Spike als Richtungssignal für einen 30m-Entry ist hoffnungslos veraltet.
- **Channel:** eigener Cornix-Trading-Channel via `telegram_outbox` (Whitelist-Rohname „Volume Indicator").
- **Cooldowns:** **keine** (P1.16) — einziger Guard ist `is_trade_already_active`; da TP1 +2,5% binnen einer Stunde treffen kann, refeuert ein historisches Spike-Event tagelang alle 30 min (Serien-Reentry → 51.440 Trades, Signalinflation per Konstruktion).

## 2. Live-Bilanz (Report 14, dedupliziert, `closed_trades_master`)
- **n = 51.440** · WR **64,1%** · ø **+0,09%**/Trade · Median **−0,10%** · Σ netto **−705** Preis-% (brutto **+4.439**).
- **Monatstrend:** Feb/Mai/Jun positiv, Mär/Apr **−7,2k** — regimeabhängig. Richtungssplit nicht separat berichtet.
- Mit besserem Exit-/Fee-Management wäre die Strategie ≈ Break-even (Report 14).
- **Scoring-Vorbehalt (Report 17):** Monitor-Scoring stimmt bei Volume Indicator nur zu **44%** mit dem First-Touch-Replay überein — schlechtester Wert der Classic-Familie, das per-Trade-Scoring ist **de facto Rauschen**. Zusätzlich **98 verworfene Outbox-Messages** im Volume-Indicator-Channel (N2).

## 3. Befunde
| ID | Schweregrad | Einzeiler | Status |
|---|---|---|---|
| P1.16 | Hoch | Kein Cooldown — bis zu 5 Tage alter Spike refeuert alle 30 min | ~ ([DB]; Step 2 zeigt md5-identische Messages 2–3× binnen 60 min im Channel) |
| P2.42 | Mittel | Ältester Spike gewinnt; Spike bei Index 0 immer „Sell"; HVN-Gate degeneriert je Tick-Size (feine Ticks: nie, grobe: immer) | ~ ([DB]) |
| P2.44 | Mittel | Volume-Strat liest 90d×30m (~4.320 Rows) pro Coin pro 30-min-Zyklus als ERSTEN Gate (~2,3M Rows/Zyklus) | ~ |
| P1.15 | Hoch | Ein schlechter Coin killt den ganzen Detector-Prozess | ~ |
| R1/05 | Hoch | Bewertet die noch laufende Kerze; Engine stempelt :02 UND :32 | ✔ (Step 2) |
| 16b | Konzept | HVN-Erkennung summiert Volumen pro exaktem Float-Close → Gate misst Tick-Size statt Volumenstruktur | ✔ (Konzeptbewertung) |

## 4. Abhängigkeiten & Querschnitts-Risiken
- **R1 Forming Candle** (Step 2 bewiesen): Spike-/HVN-Bewertung auf Partial-Kerzen.
- **R3 TZ-Mix:** betrifft Volume Indicator mangels Cooldown weniger direkt, aber die naiven Zeitstempel verzerren jede Auswertung (Replay-TZ-Alignment 50/50).
- **Monitor-Bugs P1.2/P2.7:** mit 44% Replay-Übereinstimmung ist die per-Trade-Wahrheit unbrauchbar — Whitelist-/Analyzer-Statistiken über diese Strategie erben das.
- **Outbox-Verluste (N2):** 98 der 800 still verworfenen Messages im Volume-Channel; Whitelist-Rohname seit 19.04. eingefroren (P0.4/P2.25).

## 5. Sanierungsplan
- **Sofort:** P1.16-Cooldown (12–24h per Coin oder Dedupe auf Spike-Timestamp) — halbiert die Signalinflation mit einer Änderung; P2.44-Guard-Reihenfolge (HVN-Cache, Gates umsortieren); P1.15-per-Coin-Isolation.
- **Strukturell (Umbau lt. Report 16):** gebinnte HVNs (`pd.cut` + Perzentil statt exakter Float-Preise), Frische-Anforderung an den Spike (rückwärts iterieren, i==0 skippen — P2.42), Struktur-Targets statt fixem +2,5%. Danach Monitor-Rewrite + Re-Score, dann Neubewertung; anschließend ist das **S11-Filter-Muster übertragbar** (Report 15: „Gleiches Muster danach auf Volume Indicator (51k Trades)"). Dass trotz allem brutto ein Plus bleibt, rechtfertigt den Umbau — als einzige Classic neben Support Resistance mit echtem Kern.

## 6. Belege
- `AUDIT_TODO.md`: P1.16, P2.42, P2.44, P1.15, R1, R3
- `audit_reports/05_classic_strats.md`: No-Cooldown-Refire, Spike-/HVN-Degenerierung, 90d×30m-First-Gate
- `audit_reports/14_bot_performance_db.md` §C: Zahlenzeile inkl. brutto +4.439, Monatstrend
- `audit_reports/16_strategy_concept_evaluation.md` §3: Note D+, Umbau-Verdikt
- `audit_reports/15_strategy_proposals.md`: S11-Übertragbarkeit
- `audit_reports/17_monitor_replay_and_gaps.md` §1–2: agree 44%, 98 Outbox-Verluste
- `audit_reports/STEP2_DB_VERIFICATION.md` §C P0.1: md5-identische Messages im Volume-Channel
