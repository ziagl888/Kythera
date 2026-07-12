# Modellkandidaten-Spezifikation 2026-07 — Implementierungs-Handoff

**Zweck:** Jeden Kandidaten aus dem Research-Lauf 2026-07-12
(`reports/model_ideas_research_2026-07.md`, Task T-2026-CU-9050-102) so
spezifizieren, dass ein Folge-Agent (Opus) das Coding **ohne Rückfragen**
übernehmen kann. Jeder Kandidat ist als eigener KB-Task zu schneiden
(Projekt 9050, Workflow nach `docs/OPUS-HANDOFF.md` §2).

**Pflichtlektüre vor dem ersten Edit:** `docs/OPUS-HANDOFF.md` (Arbeitszyklus,
Fallen, Eskalation) und die Arbeitsregeln in `docs/MODEL_INTENT.md` (Label muss
die Soll-Frage beantworten; `pick_threshold_safe`; beide Metriken berichten;
Ein-Job-Regel; versionierte Tags).

## 0. Regeln, die für ALLE Kandidaten gelten

1. **Batch-E-Disziplin:** Jede Idee wird zuerst billig falsifizierbar gemacht
   (Studie/Replay, ~1 Tag), bevor Live-Code entsteht. „Kein Edge" ist ein
   gültiges, zu dokumentierendes Ergebnis (No-op-Done). Vorlage:
   T-2026-CU-9050-020.
2. **DB-Arbeit nur in einer VPS-Session** (Build-Maschine hat keine
   Credentials), Live-Tabellen strikt **read-only**, Prozess-Priorität
   BELOW_NORMAL, **Ein-Job-Regel**: nur EIN Trainings-/Sim-Job gleichzeitig,
   neue Jobs hinter dem laufenden einreihen.
3. **Studien-Skripte** nach `tools/` (ruff-excluded, Lint-Bar niedriger, aber
   kein Freibrief). Ergebnisse als JSON/MD nach `staging_models/` bzw.
   `reports/`.
4. **Replay/Labels:** Immer `tools/walkforward_sim.py`-Infrastruktur bzw.
   deren Bausteine verwenden: `simulate_exit` (First-Touch TP1-vs-SL, Fees),
   `get_hvn_and_sr_levels(df=…)` + `hvn_sr_trade_geometry` +
   `ensure_min_tp_distance` für as-of-Geometrie. Chrono-Split + Purge-Gap,
   Kalibrierung + Threshold via `pick_threshold_safe` auf der Validation.
5. **Feature-Builder geteilt** (X-R1): neue Feature-Familien als
   `core/<name>_features.py`, von Studie, Trainer UND (später) Bot importiert.
   Fehlende Spalten ⇒ Load-Fehler/Idle, nie `fillna(0)` als Vertragsersatz.
6. **Nur geschlossene Kerzen** (R1); DESC-sortierte Frames beachten
   (neueste Zeile = Index 0 in manchen Pfaden). TZ: neue Tabellen
   TIMESTAMPTZ/UTC; beim Lesen von Legacy-Spalten das TZ-Cluster
   (AUDIT_TODO P2.1–P2.6) prüfen. `closed_ai_signals` enthält ~357k
   Duplikat-Zeilen — **vor jeder Auswertung deduplizieren** (per
   Signal-Identität, z. B. (coin, model, entry_time, direction) — Vorgehen im
   Skript dokumentieren).
7. **Artefakte nur nach `staging_models/`** mit `meta.model_id` = neuem Tag.
   Promotion in den Repo-Root, Gate-Flips, Bot-Entparken, Fleet-/Bot-Restarts,
   `.env`-Änderungen: **ausschließlich Michi** (OPUS-HANDOFF §6).
8. **Berichtspflicht je Studie:** n, WR, Ø-PnL netto (Fees!), Monats-Split
   (Regime-Stabilität!), Val-vs-Test-Konsistenz. WR allein ist wertlos
   (Report 16, Befund 1).
9. **Survivorship-Hinweis dokumentieren:** unsere Coin-Tabellen folgen
   `coins.json` (aktive USDT-Perps); delistete Coins fehlen teilweise.
   Jede Cross-Section-Studie vermerkt das als bekannte Bias-Quelle.
10. **Fees-Annahme einheitlich:** wie `walkforward_sim` (Taker-Fee +
    Slippage-Modell dort nachlesen und referenzieren, nicht neu erfinden).

**Datenbestand (Stand 2026-07-12):**

| Quelle | Umfang | Anmerkung |
|---|---|---|
| `{SYM}_{tf}`-Kandles | ~530 Coins × 5m/15m/30m/1h/2h/4h/1d/1w | Retention: **5m nur 1 Monat**, 15m–4h 1 Jahr (6_housekeeping) — Intraday-Studien auf **15m** aufsetzen |
| `{SYM}_{tf}_indicators` | ~120 Indikatoren | RSI/EMA/MA/WMA/SMMA u. a. |
| `funding_rates` | 430d × 530 Coins | stündlicher Backfill-Task; Builder `core/funding_features.py` (6 Features) existiert |
| `pump_dump_events` | seit 2026-02-25 | Detector-Log (vol_ratio, price_change_60s) |
| `ticker_10s` | seit 2026-07-07 | Hypertable, ~108 Coins, 10s |
| `whale_data/*.json` | seit 2026-07-05 | Top-20, Prints ≥ $25k, mit Taker-Richtung (`m`-Flag) |
| `ai_signals` / `closed_ai_signals` | volle Fleet-Historie | Duplikat-Falle s. Regel 6 |
| `ml_predictions_master` | Shadow+Live-Predictions | A/B-Basis (FIF1-Muster) |
| `regime_current` / `regime_history` | BTC-Regime 5 Klassen | TREND-Klassen erst seit §22-Umbau 2026-07-07 besetzt |
| **fehlt** | Open Interest, Liquidations, Orderbook, On-Chain | OI: nur 30d rollierend via REST → K9 |

**Empfohlene Reihenfolge** (Ein-Job-Regel; Begründung in den Specs):
K9 (zeitkritisch, Implementierung ohne Sim-Job) → K3 + K8 (billigste Studien,
reine DB-Analysen) → K1 → K2 → K5 → K4 (größter Bau) → K6 → K7 → K11;
K10/K12 warten auf Datenreife.

---

## Tier 1 — sofort testbar

### K1 · TSM1 — Time-Series-Momentum auf 6h-Aggregaten (Studie → ggf. Bot)

**Typ:** Replay-Studie, danach Operator-Entscheid über Bot. **Aufwand:** ~1–2 Tage Studie.
**Hypothese:** Ein ROC-Lookback-Signal auf 6h-Kerzen (Momentum-Mitfahren
long/short) hat über das USDT-Perp-Universum positiven Netto-Edge — auch mit
UNSERER Geometrie (Smart-Targets + fixer SL) statt des ATR-Trailings aus dem
Paper. **Evidenz:** F8 (arXiv 2602.11708v1, claimed 2,41 Sharpe netto; medium —
Overfitting-Verdacht durch monatliche Re-Optimierung).

**Vorgehen:**
1. `tools/tsmom_study.py` (neu, read-only): 1h-Kandles je Coin laden, auf 6h
   resamplen (UTC-Anker 00/06/12/18 — **nicht** lokale Zeit; nur volle,
   geschlossene 6h-Fenster). Zusätzlich denselben Lauf auf nativen
   4h-Kandles (Robustheits-Check, kein Resample-Artefakt).
2. Signal: `ROC_L = close/close[-L] − 1`. Event, wenn `|ROC_L|` einen
   Schwellwert kreuzt (Vorzeichen = Richtung). **Festes Grid, KEIN
   Re-Fitting im Zeitverlauf:** L ∈ {8, 12, 16, 24, 32} Bars ×
   Threshold ∈ {0, 0.5σ, 1.0σ} (σ = rollierende StdAbw von ROC_L, 90d,
   as-of). Dedupe: je Coin/Richtung max. 1 offenes Event (Re-Entry erst nach
   Exit), analog 4h-Cooldown-Konvention.
3. Labels doppelt: (a) unsere Geometrie via `get_hvn_and_sr_levels(df=…)` +
   `simulate_exit` (das ist die deploybare Wahrheit); (b) Paper-Approximation
   als Vergleich: Zeit-Exit nach H Bars (H ∈ {8, 16, 28}) mit weitem
   Katastrophen-SL 15 %. Divergiert (a) stark von (b), ist das die
   quantifizierte Kosten der Cornix-Substitution (Open Question 3 des
   Reports).
4. Auswertung je Grid-Zelle: n, WR, Ø-PnL netto, Monats-Split,
   Val/Test-Chrono-Split (Threshold-Wahl NUR auf Val; Test einmal anfassen).
5. Ergebnis-JSON nach `staging_models/tsmom_study.json` + MD-Kurzreport.

**Stop-Kriterium:** Keine Zelle mit Val- UND Test-positivem Netto-PnL bei
n ≥ 200 Test-Trades ⇒ Paper für unseren Stack falsifiziert, dokumentieren,
parken. **Fallen:** Resample-TZ; Survivorship (Regel 9); NICHT dem
Paper-Refitting nacheifern — genau das ist sein Overfitting-Vektor.
**Wenn positiv:** eigener Folge-Task „Bot 35 TSM1" (Tag `TSM1`, 6h-Scan-Takt,
`core/model_artifacts.py`-Loader falls ML-Gate, sonst regelbasiert;
Standard-Konventionen: EINE Cornix-Message, Cooldowns, Monitor-8-Tracking).

### K2 · XSM1/XSR1 — Cross-Section Momentum-Rotation & Alt-Pump-Reversal (Studie → ggf. Bot)

**Typ:** zweistufige Studie (Portfolio-Ebene, dann Event-Replay). **Aufwand:** ~2 Tage.
**Hypothese:** (a) XSM1: Top-Dezil der 1–2-Wochen-Returns outperformt bei
1–2 Wochen Haltedauer (LONG). (b) XSR1: Coins mit starkem 4–12-Wochen-Run
reverten (SHORT). **Evidenz:** F4 (Struktur high, exakte Spec 0-3 widerlegt —
darum Matrix statt Einzel-Spec) + F5 (Anchored-Variante, medium).

**Vorgehen:**
1. `tools/xs_momentum_study.py` (neu, read-only), 1d-Kandles:
   Formations-Fenster F ∈ {7, 14, 28, 56, 84}d × Halte-Fenster
   H ∈ {7, 14, 28}d, wöchentliches Rebalance-Raster über die 430d.
2. Ranking je Rebalance: F-Tage-Return. **Zwei Signal-Varianten:** roher
   Return UND Anchored-Variante (Distanz zum Formation-**Low**, F5).
   **Zwei Bezugsgrößen:** absolut UND marktneutral (Coin-Return minus
   BTC-Return) — sonst misst die Studie nur Beta.
3. Liquiditätsfilter: unteres Volumen-Terzil (Median-24h-Quote-Volumen über F)
   ausschließen — die Literatur-Edges leben oft in unhandelbaren Micro-Caps.
4. Stufe 1 (Portfolio): Dezil-Spreads Close-to-Close über H, netto mit
   Fees-Annahme (Regel 10) + Funding-Kosten der Short-Seite aus
   `funding_rates` (Shorts zahlen bei negativem Funding!). Heatmap F×H je
   Variante/Richtung.
5. Stufe 2 (nur für Val-positive Zellen): Event-Replay mit unserer Geometrie
   (Entry = erster 1h-Close nach Rebalance, Smart-Targets, `simulate_exit`)
   — erst das ist die deploybare Aussage.
6. Ergebnis nach `staging_models/xs_momentum_study.json` + MD-Report.

**Stop-Kriterium:** keine F×H-Zelle in Stufe 1 mit Val+Test-konsistentem
Netto-Spread ⇒ Struktur repliziert nicht auf 2024–26er Perps, dokumentieren.
**Fallen:** Survivorship (hier am stärksten!); Halte-Exits im Live-Betrieb
brauchen den Close-Command-Pfad (FMR2-Mechanik, s. K4) oder Monitor-Timeout —
Design-Entscheid gehört in den Bot-Folge-Task, NICHT in die Studie.
**Wenn positiv:** Folge-Task je Richtung (Tags `XSM1`/`XSR1`), Posting-Kadenz
wöchentlich, Kandidaten-Kappe (z. B. Top-5) als Operator-Parameter.

### K3 · FRL — Funding-Risk-Layer über die eigene Fleet (Studie → Orchestrator-Feature)

**Typ:** reine Datenanalyse, kein Modell. **Aufwand:** ~1 Tag.
**Hypothese:** Fleet-SHORTs, die bei extrem-positivem Funding eröffnet wurden,
haben systematisch schlechtere Expectancy (Squeeze-Mechanik, F2); symmetrisch
LONGs bei extrem-negativem. Das ABR2-Gate (fund_24h > +3 bps LONG /
SHORT-Veto > +1,5 bps, Report 21 Addendum 2) generalisiert fleet-weit.
**Evidenz:** F2 (high, BIS) + interner Präzedenzfall ABR2.

**Vorgehen:**
1. `tools/funding_risk_study.py` (neu, read-only): `closed_ai_signals`
   dedupen (Regel 6), je Trade `core/funding_features.py` as-of Entry
   auswerten (fund_24h, fund_72h, fund_7d_cum + Cross-Section-Perzentil des
   Entry-Zeitpunkts über alle Coins).
2. Buckets (z. B. Quintile + Extremzonen >+3 bps / <−3 bps) × Richtung ×
   Modell-Tag: n, WR, Ø-PnL, Monats-Split.
3. Report: Für welche Bots/Richtungen trennt Funding Erfolg von Misserfolg
   out-of-sample über die Monate? (Chrono-Halbierung als Pseudo-Val/Test.)

**Stop-Kriterium:** kein Bucket-Effekt, der über beide Zeit-Hälften stabil ist
⇒ ABR-Befund generalisiert nicht, dokumentieren. **Wenn positiv:** Folge-Task
„Funding-Dimension im Orchestrator-Gating (Bot 28)" — Achtung: das ist ein
Gating-Parameter-Change ⇒ **Operator-Gate, Michi entscheidet** (OPUS-HANDOFF
§6). **Fallen:** BIS-Konvention „sell liquidations" = Short-Seite (invertiert
zu Vendor-Dashboards); TZ der Entry-Timestamps.

### K4 · FMR2 — Funding-Extreme-MR mit Normalisierungs-Exit (Bau nach fertigem Design)

**Typ:** Builder + Retrain + (bei Erfolg) Bot-Exit-Loop. **Aufwand:** ~2–3 Tage
über mehrere Queue-Slots. **Design liegt vollständig in
`docs/NEW_IDEAS_BOTS.md` § „FMR2 — eigener Exit-Pfad"** — dieses Kapitel ist
bindend; hier nur die Reihenfolge und Ergänzungen:

1. Exit-Predicate + Konstanten nach `core/research_features.py` (eine Quelle
   für Builder UND Bot): SHORT-Exit sobald `funding_cs_pctl < 0.80` ODER
   `funding_z_30d < 1.0`; LONG symmetrisch; Time-Stop 9 Settlements (3 Tage);
   harter Katastrophen-SL bleibt.
2. `tools/fmr1_build_dataset.py` V2: Label = PnL am Normalisierungs-/
   Timeout-Exit (Exit-Preis der Settlement-Kerze) — NICHT First-Touch-TP/SL
   (das war der FMR1-Fehler).
3. Retrain via `tools/new_models_train.py`-Gerüst (Chrono-Split, Purge,
   `pick_threshold_safe`), Artefakt `staging_models/fmr2_model_*.pkl`,
   `meta.model_id=FMR2`.
4. NUR bei Val+Test positiv: Bot-31-Exit-Loop (Close-Command via
   `send_telegram` → `telegram_outbox`, eigene Rows per
   `DELETE … RETURNING` → `closed_ai_signals`
   `status='CLOSED_FUNDING_NORMALIZED'`, Filter strikt auf eigenen Tag;
   stündlicher Scan reicht). Eigener Channel via `CH_FMR1` (.env = Michi).

**Stop-Kriterium:** Val+Test nicht positiv ⇒ S8-These endgültig falsifiziert
(dann ist auch der Bot-Umbau obsolet — offline-first war der Sinn der
Reihenfolge). **Evidenz-Kontext:** F3 — Perp-only-Funding-Capture ist nach der
Post-ETF-Kompression genuin offen; genau deshalb ist der saubere Test wertvoll.

---

## Tier 2 — sofort testbar, mittlere Evidenz

### K5 · LIS1 — Post-Listing-Drift (Studie → Risk-Filter und/oder Fade-Bot)

**Typ:** Kohorten-Studie + Replay. **Aufwand:** ~1 Tag.
**Hypothese:** Frisch gelistete Perps underperformen in den ersten Wochen bis
Monaten (F10). Minimal-Nutzen: **LONG-Blacklist für junge Listings** (reiner
Risikofilter); Maximal-Nutzen: Fade-SHORT ab Tag T nach Listing.

**Vorgehen:**
1. Listing-Datum je Coin: einmalig `GET /fapi/v1/exchangeInfo`
   (`onboardDate`, UTC) ziehen und als JSON nach `staging_models/` cachen;
   Fallback-Proxy: erste Kerze der 1h-Tabelle.
2. `tools/listing_drift_study.py` (neu): Kohorte = onboardDate im
   Datenfenster; Forward-Returns Tag 1→7/30/90/180 absolut UND minus BTC
   (Beta-Confound der Quellen beheben!); Verteilung, Median, % positiv.
3. Fade-Replay: Entry-Varianten Tag {3, 7, 14} nach Listing (Limit +0 %/+5 %),
   Smart-Targets SHORT, `simulate_exit`; **Funding-Kosten zwingend
   einrechnen** — frische Perps haben oft extremes Funding, das die
   Short-Seite bezahlen kann.
4. Report inkl. Kohorten-Größe (bei ~40–60 Listings/Jahr ist n klein — ehrlich
   ausweisen, keine Signifikanz vortäuschen).

**Stop-Kriterium:** Drift verschwindet nach Beta-Adjust oder n zu klein für
eine Aussage ⇒ nur den deskriptiven Befund dokumentieren. **Minimal-Deliverable
auch ohne Short-Edge:** quantifizierte Empfehlung „Coin-Alter < X Tage ⇒ kein
LONG" als Orchestrator-/Bot-Filter (Umsetzung = Gating-Change ⇒ Michi).

### K6 · BRD — Markt-Breadth/Dispersion als Regime-Features (Feature-Block + Studie)

**Typ:** geteilter Feature-Builder + Validierungs-Studie. **Aufwand:** ~1–2 Tage.
**Hypothese:** Breadth-Größen über das 530er-Universum (Anteil Coins > EMA200/
EMA50, Median-7d-Return, Advance/Decline, Return-Dispersion vs. BTC) schlagen
bzw. ergänzen die BTC-only-Regime-Klassifikation — und liefern das fehlende
**Regime-Gate für RUB-LONG** (MODEL_INTENT §8: TREND_UP +1,65 %/Trade,
n=1.378, 9/13 Monate positiv). **Evidenz:** extern unbeforscht (Report §3
Frage 4); intern stark motiviert (§8/§22/§23, HMM-Task T-2026-CU-9050-020).

**Vorgehen:**
1. `core/breadth_features.py` (neu, X-R1): as-of-Builder auf 1d/1h-Kandles +
   `_indicators` (EMA200 liegt vor). Effizienz: je Coin EINE Query, in-memory
   aggregieren — nicht 530 Tabellen je Zeitpunkt einzeln hämmern;
   BELOW_NORMAL.
2. `tools/breadth_study.py`: (a) Features vs. Forward-Returns der
   RUB-LONG-Events aus `rub_replay_365d.jsonl` (liegt vor — kein neuer
   Sim-Lauf nötig!); (b) Features vs. `regime_history`-Klassen
   (Zusatzinformation ja/nein, einfache Logit/Tree-Diagnostik reicht);
   (c) Monats-Split.
3. Bei Befund Folge-Tasks: Feature-Einspeisung in den Whitelist-Umbau (§23),
   die HMM-Studie (T-020) und/oder ein expliziter TREND/Breadth-Schalter für
   RUB-LONG in Bot 13 (**Gate-Änderung ⇒ Michi**).

**Stop-Kriterium:** kein Feature trennt RUB-LONG-Monate out-of-sample besser
als das bestehende Regime ⇒ dokumentieren; die Builder-Arbeit bleibt als
Infrastruktur trotzdem nützlich (HMM-Task) — das ist dann bewusst zu
entscheiden, nicht stillschweigend.

### K7 · MOM — Realized-Moments-Feature-Block + Skewness-Studie (SKW1)

**Typ:** Feature-Builder + Cross-Section-Studie. **Aufwand:** ~1–2 Tage.
**Hypothese:** (a) Realized Skewness (rolling, Intraday-Basis) prädiziert
negativ ⇒ Short-Kandidatenfilter (SKW1); (b) RV/Kurtosis als zusätzlicher
Feature-Block für kommende Retrains (ATS2, QM2, BR-Gate). **Evidenz:** F7
(medium, zwei unabhängige Papers; Mechanik-Story widerlegt — nur die
Vorzeichen verwenden, keine Story).

**Vorgehen:**
1. `core/moment_features.py` (neu, X-R1): realized vol/skew/kurt aus
   **15m**-Kandles (5m hat nur 1 Monat Retention — 15m = 1 Jahr!), rollierende
   Fenster {24h, 7d}, as-of, nur geschlossene Kerzen; NaN-Politik nativ
   (XGB-Muster P1.20).
2. `tools/skewness_study.py`: wöchentliche Dezil-Sorts (Methodik-Gerüst von
   K2 wiederverwenden: marktneutral, Liquiditätsfilter, Funding-Kosten),
   Richtung: Short-High-Positive-Skew vs. Long-Low-Skew; plus
   RV/Kurtosis-Sorts als Nebenprodukt.
3. Feature-Block-Integration: optionaler `--features moments`-Anschluss in
   `tools/retrain_from_replay.py` analog zum Funding-Block (6 Funding-Features
   als Vorbild) — **nur den Anschluss bauen, kein Retrain triggern** (Queue).

**Stop-Kriterium:** Skew-Dezile ohne stabilen Netto-Spread ⇒ SKW1 tot; der
Feature-Block bleibt als Retrain-Option bestehen (Verwendung entscheidet der
jeweilige Retrain-Task). **Falle:** MAX-basierte Shorts sind durch F6
kontraindiziert — nicht „aus Versehen" MAX statt Skewness bauen.

### K8 · SET — Settlement-/Tageszeit-Studie über die eigene Fleet

**Typ:** reine Datenanalyse. **Aufwand:** ~0,5 Tage (billigste Studie im Katalog).
**Hypothese:** Entry-Nähe zu den Funding-Settlements (00/08/16 UTC) bzw.
Tageszeit-Fenster beeinflusst die Expectancy unserer Trades (F9: Spread-/
Vol-Muster um Settlements). **Evidenz:** F9 (medium, nur 2 Monate Daten,
Dispersion ≠ Returns — darum testen wir auf UNSEREN Trades).

**Vorgehen:** `tools/settlement_timing_study.py` (neu, read-only):
`closed_ai_signals` dedupen; je Trade Entry-Offset zum nächsten Settlement
(−240…+240 min in 30-min-Buckets) + Entry-Stunde UTC; Expectancy je Bucket ×
Richtung × Modell-Tag; Bootstrap-CI (einfaches Resampling, keine
Signifikanz-Theater). **TZ-Falle:** Entry-Timestamps teils naiv-lokal —
TZ-Cluster P2.1–P2.6 lesen, Offsets DST-aware konvertieren (f95f092-Muster).

**Output:** Empfehlungs-Tabelle „Bot × Fenster meiden/bevorzugen" — Umsetzung
(Scan-Minuten-Verschiebung o. Posting-Fenster) je Bot als Mini-Follow-ups.
**Stop:** keine stabilen Buckets ⇒ dokumentieren, fertig.

---

## Tier 3 — Daten säen jetzt, ernten später

### K9 · OIC — Open-Interest-Collector ⚠ ZEITKRITISCH (Infrastruktur)

**Typ:** Collector-Prozess + Hypertable. **Aufwand:** ~1 Tag. **Warum zuerst:**
Binance REST hält OI-Historie nur ~30 Tage — jeder Tag ohne Collector ist
unwiederbringlich verlorene Historie (Backfill unmöglich; dieselbe Lektion wie
ticker_10s: ehrlich akkumulieren statt Quellen-Skew).

**Vorgehen (Blaupause = `core/ticker_10s.py`):**
1. `core/oi_5m.py` (neu): Hypertable `oi_5m` (`ts TIMESTAMPTZ NOT NULL,
   symbol TEXT NOT NULL, open_interest DOUBLE PRECISION, oi_value_usdt DOUBLE
   PRECISION, PRIMARY KEY (ts, symbol)`); Timescale-Jobs: Chunks 1 Tag,
   Compression nach 3 Tagen (segmentby=symbol), Retention 730 Tage;
   Kill-Switch `KYTHERA_OI_PERSIST=0` (Default an); batched Insert.
2. Writer: **eigener schlanker Prozess** `35_oi_collector.py` (kein Anbau an
   Detector — getrennte Failure-Domain): alle 5 min ein Sweep über
   `coins.json`-Symbole via `GET /futures/data/openInterestHist`
   (period=5m, limit=1, weight klein) ODER `GET /fapi/v1/openInterest`;
   Rate-Budget dokumentieren (530 Requests/5 min ≪ 2400 weight/min, mit
   Backoff-Retry nach `core`-Konventionen). Registrierung in `core/fleet.py`
   (+2 PG-Connections je Prozess — P1.34 gegen `max_connections` prüfen).
3. **Initial-Backfill einmalig:** die verfügbaren ~30d `openInterestHist`
   (period=5m, paginiert) je Symbol einlesen — mehr gibt die API nicht her.
4. Start des Prozesses = Fleet-Eingriff ⇒ **Michi** (Restart-Marker-Mechanik
   `control/restart/` bzw. Watchdog nimmt neue `core/fleet.py`-Einträge beim
   nächsten Zyklus? — verifizieren; im Zweifel Operator-Restart).

**Modell-Ideen darauf (eigene Tasks ab ~Okt 2026, ≥60d Historie):**
OI-Preis-Divergenz (Preis↑ + OI↓ = schwacher Move ⇒ Fade), OI-Spike-Fade,
OI×Funding-Interaktion (F2-Mechanik verfeinert: Squeeze-Anfälligkeit =
hohes OI + extremes Funding).

### K10 · WHI — Whale-Print-Imbalance (Studie, wartet auf Datenreife)

**Typ:** Persistenz-Review jetzt, Studie ab ~4–6 Wochen Historie.
**Hypothese:** Taker-Richtungs-Imbalance großer Prints (≥$25k, `m`-Flag im
aggTrade-Stream von Bot 19) über 5/15/60-min-Fenster prädiziert kurzfristige
Forward-Returns auf den Top-20. **Evidenz:** extern unbeforscht (Report §3
Frage 4); Daten seit 2026-07-05.

**Jetzt machbar (kleiner Task):** `whale_data/*.json`-Format sichten; optional
Persistenz nach Hypertable `whale_trades` (Query-Komfort, gleiche
Timescale-Konventionen wie K9) — der Logger schreibt weiter JSON, ein
Migrations-Skript liest nach. Universums-Erweiterung über Top-20 hinaus =
mehr WS-Streams ⇒ **Operator-Entscheid**.
**Studie (später):** Imbalance-Features as-of vs. Forward-Returns 15m/1h/4h;
bei Signal → Feature für BTC-Regime/ROM1 oder eigener Kandidat.

### K11 · WSH1 — Wick-basierte Stop-Hunt-Reversals (Studie)

**Typ:** Event-Studie + Replay. **Aufwand:** ~1 Tag.
**Hypothese:** Kerzen mit extremer Docht-Geometrie + Volumen-Klimax
(Liquidation-Cascade-Proxy ohne Liquidation-Feed) markieren kurzfristige
Reversal-Punkte: langer unterer Docht → LONG-Bounce (Spiegel: oberer Docht →
SHORT). **Evidenz:** extern nur Mechanik (TradingView „Liquidation Cascade
Detector" — Performance-Claims ignorieren, F11/F12); intern: PEX1-Lektion.

**Vorgehen:**
1. `tools/wick_reversal_study.py` (neu) auf **15m**-Kandles (5m-Retention!):
   Event-Definition parametrisiert: `lower_wick ≥ k×ATR14` (k ∈ {1.5, 2, 3}) ×
   `volume ≥ m×vol_sma20` (m ∈ {3, 5}) × Close-Recovery ≥ 50 % des Dochts.
   Entry = Close der Event-Kerze (geschlossen!), Richtung mit dem Bounce.
2. Zwei Populationen: alle Events vs. Events ≤ 60 min nach einem
   `pump_dump_events`-Eintrag (Cascade-Kontext) — trennt „irgendein Docht"
   von „Docht nach Kaskade".
3. Labels: `simulate_exit` mit Smart-Targets; Report nach Standard (Regel 8).

**Stop-Kriterium:** keine Parameter-Zelle Val+Test-positiv ⇒ falsifiziert.
**PEX1-Lektion beachten:** Informationsgehalt steckt im Intraday-Fenster um
das Event — NICHT auf 1h-Kontext-Features ausweichen; wenn 15m zu grob wirkt,
ist das Warten auf ticker_10s-Reife (PEX2-Pfad) die Antwort, nicht 1h.

### K12 · TRM2 — Transition-Resolution-Wiedervorlage (entblockt)

**Typ:** Wiedervorlage eines geparkten Kandidaten. **Aufwand:** ~0,5 Tage wenn reif.
TRM1 war upstream blockiert (Detector hielt TREND nie — Klassen existierten
nicht, `docs/NEW_IDEAS_BOTS.md`). Seit dem §22-Umbau (2026-07-07,
Mid-Band-Regel V2 K=1,5 + Hysterese) treten TREND_UP/DOWN je ~10 % der Zeit
auf. **Trigger-Bedingung:** `regime_history` zählen — ≥300 abgeschlossene
TRANSITION→TREND-Übergänge je Zielklasse seit 2026-07-07 (realistisch einige
Wochen). Dann: `tools/trm1_build_dataset.py` erneut (prüfen, ob der Builder
die neuen Klassen sauber sieht), `new_models_train.py --strategy trm
--min-val-trades 20`, Tag **TRM2**. Stop-Kriterien wie gehabt
(Deploy-Gate in NEW_IDEAS_BOTS.md).

---

## Nicht verfolgen (dokumentierte Anti-Kandidaten)

| Idee | Warum nicht | Beleg |
|---|---|---|
| Delta-neutrale Funding-Arb (long spot / short perp) | braucht Spot-Leg — nicht im Cornix-Stack; post-ETF komprimiert, 2024/25-Profitabilität beidseitig umstritten | F3, Refuted-Liste |
| Naiver Lottery-Short auf High-MAX-Coins | MAX-Effekt in Krypto invertiert; wenn Lottery-Short, dann Skewness (K7) | F6/F7 |
| Equity-Style 3–12-Monats-Momentum | flippt in Krypto ab ~1 Monat in Reversal | F4 |
| BB/KC-Squeeze als eigenständiges Modell | Community-populär, Performance-Evidenz null; höchstens als billige Nebenzelle in K1s Grid | F11 |
| TradingView-„Winrates" als Evidenz übernehmen | >95 % Repaint; nur Mechaniken als Hypothesen verwenden | F12, Report 16 |
| PEX1-Rehabilitation auf 1h-Features / EPD2-Retrain ohne Alt-Pump-Fenster / RUB2-LONG als Event-Gate / SRA2 vor Label-Pipeline-Fix | intern bereits sauber falsifiziert — Recherche liefert KEINE neue Rehabilitierungs-Evidenz | MODEL_INTENT §7/§8, NEW_IDEAS_BOTS |

## Task-Zuschnitt (Vorschlag für die KB)

Je Kandidat EIN Task (Titel-Schema „K<N> <Tag>: <Kurzziel> (Studie|Bau)"),
`touches` deklarieren, Reihenfolge aus §0. K4 (FMR2) und K9 (OIC) sind
Implementierungs-Tasks mit Eskalationspunkten (Channel-.env, Fleet-Prozess);
alle anderen starten als Studien-Tasks, deren Folge-Tasks erst nach positivem
Befund geschnitten werden. Kein Kandidat deployt irgendetwas ohne Michis
expliziten Entscheid.
