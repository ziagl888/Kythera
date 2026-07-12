# Research-Report: Modellideen für Kythera aus externer Evidenz (2026-07-12)

**Auftrag:** Michi, 2026-07-12 — „anhand unserer Datensammlung und Diskussionen auf
TradingView und Reddit weitere Modellideen entwickeln, die wir testen und ggf.
implementieren können."
**Methode:** Deep-Research-Workflow (Run `wf_d0266aa1-319`, 101 Sub-Agents):
5 Such-Winkel (academic-funding-carry, academic-cross-sectional,
practitioner-tradingview-pine, reddit-community-consensus,
microstructure-from-OHLCV) → 19 Quellen gefetcht → 91 Claims extrahiert →
Top-25 adversarial verifiziert (je Claim 3 unabhängige Refutation-Votes;
2/3-Refutes killen). Ergebnis: **20 bestätigt, 5 widerlegt, 0 unverifizierbar.**
**Task:** T-2026-CU-9050-102. Die implementierungsreifen Specs je Kandidat
liegen in `docs/MODEL_CANDIDATES_SPEC_2026-07.md`.

**Constraints, gegen die recherchiert wurde:** OHLCV 5m–1w für ~530
Binance-USDT-Perps (~430d), ~120 Indikatoren, volle Funding-Historie,
pump_dump_events seit Feb 2026, ticker_10s + Whale-Prints (>$25k, Top-20) erst
seit ~05./07.07.2026; **kein** OI-Archiv, keine Liquidation-Feeds, kein
Orderbook, kein On-Chain. Execution: Telegram → Cornix → Binance (Entry
market/limit, TP-Ladder, fixer SL; Close-Command möglich; kein HFT, kein
ATR-Trailing).

---

## 1. Bestätigte Befunde (Confidence, Quelle, Verify-Vote)

### 1.1 Funding/Carry — die stärkste Evidenzlage

**F1 · Crypto-Carry-Premium ist real, groß, persistent — aber der reine Harvest
braucht ein Spot-Leg.** Ø ~7 % p.a. über Börsen (Apr 2019–Jul 2024; BTC-Basis
8,2 %/6,4 % OKEx/CME, Spitzen 45–55 %), ~10× S&P-500-Carry. Das Short-Futures-Leg
ist so volatil (17 %/Monat StdAbw vs. 2–3 % Mittel), dass 10x isoliert in >50 %
der Monate liquidiert worden wäre. Post-ETF (Jan 2024) um ~3–5 pp komprimiert.
— *high*, BIS WP 1087 (Schmeling/Schrimpf/Todorov), verbatim gegen das
Primär-PDF verifiziert, Votes 3-0/3-0.

**F2 · Erhöhtes Funding ist ein Vorwärts-Risikoindikator für Short-Squeezes:**
+10 % standardisierter Carry ⇒ +22 % Short-Seiten-Liquidationen (% des OI) im
Folgemonat; Long-Seite nicht prädiziert. Direkt als Richtungs-/Risikofilter auf
unserer 430d×530-Funding-Historie implementierbar. **Achtung Konvention:** BIS
„sell liquidations" = Short-Seite — invertiert zur Coinglass/Binance-Vendor-
Konvention. — *high*, BIS WP 1087 S. 25, Vote 3-0.

**F3 · Perp-Spot-Abweichungen sind strukturell** (geschlossene No-Arbitrage-Form,
Band unter Handelskosten): historisch 60–90 %/Jahr mittlere absolute Abweichung
(2020–2022), Spot-gehedgte Funding-Arb Sharpe ~1,8 (Retail) bis 3,5 (MM).
**Implementierbarkeits-Lücke:** dokumentiert ist delta-neutral long-spot/
short-perp — mit unserem Perp-only-Stack nicht 1:1 replizierbar; direktionale
Funding-Capture ist nur ein Teil-Proxy. Sample endet Dez 2022; post-ETF stark
komprimiert. Die Gegen-These (CEX-Funding-Arb 2024/25 negativ) wurde 1-2
angezweifelt — **die aktuelle Profitabilität ist in beide Richtungen offen.**
— *high* (für die historische Aussage), arXiv 2212.06888v5 + SSRN 4301150,
Votes 3× 3-0.

### 1.2 Cross-Section über das große Universum

**F4 · Krypto-Momentum lebt nur 1–4 Wochen und flippt ab ~1 Monat in
signifikantes Reversal** — viel schneller als Aktien. Der 1–2-Wochen-Return-Sort
ist das extremste Signal (kurz gehalten Momentum bis ~70 % p.a.; 10–12 Wochen
gehalten stärkstes Reversal). **Aber:** die konkrete Parametrisierung „2w/2w ist
die beste Spec, 1w/1w insignifikant" wurde 0-3 **widerlegt** — nur die
Horizont-Struktur ist belastbar, nicht die Parameter. Pre-2021-**Spot**-Daten
(~2.000 Coins, inkl. illiquider Micro-Caps), brutto, Loser-Leg schwer
shortbar. — *high* (Struktur), Dobrynskaya SSRN 3913263 (peer-reviewed JAI
2023), Votes 3-0/3-0.

**F5 · Anchored Reversal:** Reversal-Sortiersignal zerlegt am **Formation-Low
als Verhaltens-Anker** schlägt plain-past-return-Reversal; Autoren claimen
Kosten-Robustheit über 30–360d-Formationen. Nur Abstract verifizierbar
(Paywall), Single Study, FRL-Kurzformat, Spot-Daten. — *medium*,
Nakagawa & Sakemoto FRL 2025, Votes 3-0/3-0.

**F6 · MAX-/Lottery-Effekt ist in Krypto INVERTIERT:** höheres Max-Tagesreturn
prädiziert HÖHERE Folge-Returns („lottery-like momentum"), unabhängig von
konventionellem Momentum. Der aus Aktien importierte „Short die Lottery-Coins"-
Reflex aus Trading-Communities widerspricht der Krypto-Literatur.
Size-abhängig (Gegenbefund in Top-20-Caps), pre-2021, 20–64 Coins.
— *medium*, NAJEF 2021 + IRFA 2021 (+3,03 %/Woche H-L-MAX-Dezil) + Financial
Innovation 2021, Votes 2-1/3-0.

**F7 · Realized Moments prädizieren die Cross-Section:** RV und Kurtosis
positiv, **Skewness negativ** (hoch-positiv-schiefe Coins underperformen).
Wenn Lottery-Short, dann über realized Skewness — nicht über MAX. Mechanik-
Story („getrieben von extremen Positiv-Returns") wurde 0-3 widerlegt. Momente
aus Intraday-Kandles berechenbar. — *medium*, FRL 2021 (84 Coins) + IRFA 2024
(Korroboration), Votes 2-1/2-1.

### 1.3 Direkt auf unser Universum passende Backtests

**F8 · TSMOM auf 6h-Kerzen, 150+ Binance-USDT-Perps:** ROC-Lookback ×
Entry-Threshold, long/short, ATR-Trailing-Exit; claimed **2,41 Sharpe netto**
(40,5 % p.a., −12,7 % MDD, Jan 22–Dez 24, 4 bps Fees+Slippage+Funding; >2,0 bei
8 bps). **Zwei harte Caveats:** (a) Single-Author-Preprint, monatliche
Grid-Re-Optimierung aller 3 Parameter = klassischer Overfitting-Vektor,
Survivorship unbehandelt; (b) ATR-Trailing-Exit ist mit Cornix nicht nativ
abbildbar. Es ist trotzdem der beste Daten-Match im ganzen Suchraum (unser
Universum, unser Zeitraum) und billig in-house zu falsifizieren. — *medium*
(Verifikation der Claims verbatim, Vertrauen in die Performance begrenzt),
arXiv 2602.11708v1, Votes 3-0/3-0.

### 1.4 Zeit- und Ereignis-Effekte

**F9 · Settlement-gebundene Intraday-Struktur existiert:** Cross-Exchange-Spreads
peaken ~2 h nach den 8h-Settlements (00/08/16 UTC), Tagesmax 02:00 UTC. Gemessen
auf Cross-Venue-**Dispersion**, nicht Single-Exchange-Returns; nur ~2 Monate
Daten; ~2,5 bps Peak-zu-Tal. Motiviert Entry-**Timing**, kein Standalone-Edge.
— *medium*, Zhivkov et al., IJFS (MDPI) 14(5):103, Apr 2026, Vote 3-0.

**F10 · Neue Binance-Listings driften systematisch negativ:** n=31-Sample nur
~16 % nach 6 Monaten im Plus; korroboriert durch 44-Listing-2024-Studie
(−22,7 % @3M, −37,6 % @6M, 5,5 % positiv) und 2025-Sample (89 % negativ). Der
quantifizierte Short-Edge (−18 %-Drawdown-Claim) wurde 1-2 angezweifelt —
Richtung ja, Größe/Timing offen; kein Beta-Adjust in den Quellen. — *medium*,
FXStreet 2024 + Traders Union + BeInCrypto, Vote 3-0 (Companion-Claim 1-2 ✗).

### 1.5 Community-Mechaniken (Evidenz-Gefälle)

**F11 · BB-inside-KC-Squeeze (TTM):** mechanisch sauber, OHLCV-only,
Closed-Candle-tauglich — aber **null glaubwürdige Performance-Evidenz**; die
eine gefundene „Optimierung" ist ein 243-Kombinationen-Sweep (Lehrbuch-
Overfitting). Community-populär ≠ belegt. — *low*, PyQuantLab/StockCharts/
TrendSpider, Vote 3-0 (nur Mechanik).

**F12 · TradingView-Repaint-Warnung (bestätigt unsere Report-16-Falle):**
>95 % der Pine-Indikatoren repainten technisch (Live-Wert vor Bar-Close nicht
final; `lookahead_on` bei HTF-Requests erzeugt unmögliche Backtests).
Publizierte TradingView-„Winrates" sind als Evidenz wertlos; Mechaniken
können trotzdem als Hypothesen dienen. — Kontext-Befund aus dem
practitioner-Winkel.

**F13 · Negativ-/Scoping-Befund:** Die Funding-Mechanism-Design-Literatur
(Kim & Park, arXiv 2506.08573) ist rein theoretisch — keine Backtests, kein
Strategie-Beweis. Nur als Begründung, WARUM Funding existiert. — *high*,
Volltext geprüft, Vote 3-0.

## 2. Widerlegte Claims (im Verify gekillt — nicht darauf bauen)

| Claim | Vote | Quelle |
|---|---|---|
| CEX-Funding-Arb 2024/25 hatte NEGATIVE Sharpe (−7,3/−7,9) | 1-2 ✗ | ScienceDirect S2096720925000818 |
| Funding-Arb-Absolutreturns winzig (2,2 % Binance vs. 113 % HODL) | 1-2 ✗ | dito |
| „2w/2w ist die beste Momentum-Spec; 1w/1w insignifikant" | 0-3 ✗ | SSRN 3913263 |
| „Higher-Moment-Prädiktion kommt von extremen Positiv-Returns (Equity-MAX-Analogie)" | 0-3 ✗ | FRL S1544612320303135 |
| „Listing-Day-Long = −18 % Ø-Drawdown ⇒ Fade hat positiven Erwartungswert" | 1-2 ✗ | FXStreet |

Die 1-2-Votes heißen: umstritten, nicht sicher falsch — aber als Fundament
unbrauchbar.

## 3. Offene Fragen (extern unbeforscht, nicht widerlegt)

1. Replizieren die pre-2021-Spot-Effekte (Momentum→Reversal-Struktur, MAX-Sign,
   Skewness-Sign) auf 2024–26er Binance-Perps nach realistischen Cornix-Fees?
   → genau das testen unsere Studien in `docs/MODEL_CANDIDATES_SPEC_2026-07.md`.
2. Ist direktionale Perp-only-Funding-Capture nach der Post-ETF-Kompression noch
   profitabel? (Evidenz in beide Richtungen umstritten.)
3. Überlebt der TSMOM-Edge die Substitution ATR-Trailing → Fixed-SL/TP-Ladder
   plus Survivorship-Korrektur?
4. Such-Winkel **ohne** überlebende Claims: Whale-Print-/Volume-Signale,
   BTC-Dominanz/Breadth-Gating, OHLCV-Wick-Liquidation-Cascades,
   OI-Strategien. Unbeforscht ≠ widerlegt — Areas, die direkt auf Daten mappen,
   die wir schon sammeln (pump_dump_events, whale_data) oder jetzt sammeln
   sollten (OI).

## 4. Quellen (19 gefetcht, Qualitäts-Label aus dem Workflow)

Primär: BIS WP 1087 · arXiv 2212.06888v5 · SSRN 3913263 · arXiv 2602.11708v1 ·
FRL S154461232501058X (Anchored Reversal) · NAJEF S1062940821001625 (MAX) ·
FRL S1544612320303135 (Moments) · MDPI IJFS 14(5):103 (Settlement) ·
arXiv 2506.08573 (Theorie) · ScienceDirect S2096720925000818 (Funding-Arb-Kritik) ·
SSRN 4301150. Sekundär/Blog/Forum: FXStreet (Listings) · PyQuantLab ·
FMZQuant-TTM · crosstrade.io (Repaint) · techacademies-Medium ·
awesome-pinescript (GitHub) · TrendSpider · TradingView „Liquidation Cascade
Detector [QuantAlgo]".

Hinweis: In §1 zusätzlich genannte Korroborations-Quellen (IRFA 2021/2024,
Financial Innovation 2021, Traders Union, BeInCrypto) stammen aus den
Verify-Läufen bzw. aus Zitaten INNERHALB der 19 gefetchten Quellen — sie
zählen nicht zur Fetch-Liste.

## 5. Einordnung gegen den internen Bestand

Deckungen: F2 validiert extern das ABR2-Funding-Gate/SHORT-Veto (Report 21
Addendum 2, kreuzvalidiert auf 33,5k Events). F3 motiviert FMR2 (Design liegt,
NEW_IDEAS_BOTS.md) als saubersten Test der Perp-only-Capture-These. F8/F4
sind mit unserer Replay-Infrastruktur (walkforward_sim, simulate_exit,
retrain_from_replay) je ~1 Studientag falsifizierbar. Bereits intern
Falsifiziertes wird durch die Recherche NICHT rehabilitiert: PEX1
(1h-Features informationslos), EPD2 (kein Alt-Pump-Fenster), RUB2-LONG als
Event-Gate (Regime-Problem), FMR1 (falsche Label-Geometrie — F3/FMR2 ist der
korrekte Retest).
