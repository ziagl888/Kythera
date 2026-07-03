# 14 — Bot-/Strategie-Ergebnisse aus der Live-DB (Step 4)

**Stand:** 2026-07-03 · **Quelle:** `closed_ai_signals` (AI-Bots) + `closed_trades_master` (Classic) auf dem Live-VPS. `closed_trades`/2–5 sind die eingefrorene v1-Generation (bis 24.02.) und wurden nicht neu ausgewertet.

**Methodik & Vorbehalte (wichtig fürs Lesen der Zahlen):**
- PnL = Preisbewegung Entry→Close in %, richtungsbereinigt, **ohne Leverage** (Margin-PnL wäre lev-fach) und pauschal −0,10% Round-Trip-Fee für „netto".
- „Win" = mindestens TP1 berührt (targets_hit ≥ 1 bzw. Classic-Status 1–4/SL1–3). **Achtung: Win ≠ profitabel** — genau das zeigen die Daten (s.u.).
- Alle Zahlen sind **monitor-generiert** und erben die bekannten Monitor-Bugs: P1.2 (Trailing-SL zieht nie nach → Multi-Target-PnL verzerrt), P2.7 (nur jüngste 5m-Kerze geprüft → verpasste Hits bei Downtime), P2.31 (Monitor scored bis 21 Targets, publiziert werden TP1–5), P1.9 (Regime-Close zensiert fremde Trades). Sie sind das ehrlichste verfügbare Maß, aber kein Ersatz für einen Exchange-Abgleich.

---

## A. Integritäts-Befunde (zuerst, weil sie jede Statistik betreffen)

1. **🔴 82% von `closed_ai_signals` ist Migrations-Müll.** 357.483 von 434.396 Rows sind Duplikate: 7.210 Gruppen mit identischem (symbol, model, direction, open_time), Extremfall BULLAUSDT/EPD1/SHORT mit **2.327 Close-Rows für ein Signal**. 364.641 Rows tragen den Sentinel-Zeitstempel `open_time = 2026-02-24 12:43:59.65` (v2-Go-Live-Moment), und die „LEGACY …"-Re-Closes stammen komplett vom 01.–02.03. — ein einmaliges Migrations-/Re-Scoring-Ereignis, das dieselben Alt-Trades hundertfach schloss. Nach Dedup bleiben von 352.315 LEGACY-Rows noch **12.646 echte** (ø −0,88%). **Fix:** Unique-Index auf `(symbol, model, direction, open_time)` + einmaliger Purge; bis dahin jede Auswertung deduplizieren. Die aktive Ära ist davon fast unberührt (nur AIM1: 3.125→3.047).
2. **Classic:** 11.383 Duplikat-Gruppen (~11k überzählige Rows, klein relativ zu 363k). 162.941 Rows ohne `close_price` sind **ausschließlich Alt-Ära ≤ 28.02.** — seit v2 wird vollständig geschrieben.
3. Status-vs-PnL-Konsistenz Classic: 2.918 Rows (1,6%) mit Win-Status aber PnL < −0,5% (Trailing-Give-back/P1.2-Effekt); umgekehrt 0.
4. In `closed_ai_signals` existieren tote Namensvarianten (`MIS1-72h_dump`, `MSI1-*`, `ATS1_Robust`), zu 100% zensiert — Alt-Vokabular, sollte beim Purge mit raus.

---

## B. AI-Bots — aktive Ära (24.02.–03.07.), dedupliziert, n=59.823

Gesamt: **WR 61,1%, ø +0,77%/Trade brutto (+0,67% netto), Summe +45.827 Preis-% (netto +39.844)**.

| Modell | n | WR | ø PnL | Median | Σ netto | Urteil |
|---|---|---|---|---|---|---|
| MIS1-72H | 11.822 | 63,9% | +1,44% | 0,00 | **+15.868** | Arbeitspferd; in jedem Monat positiv |
| EPD1 | 4.392 | 72,8% | +3,34% | +3,63 | **+14.222** | stärkster ø; fast alles aus Mai/Jun (+14,6k), Jul negativ (−345) |
| MIS1-168H | 7.167 | 58,5% | +1,07% | −0,03 | +6.928 | positiv, aber seit Mai schwächelnd (WR 48/49/35) |
| RUB1 | 2.496 | 57,6% | +1,57% | −0,06 | +3.675 | Summe aus Tail-Gewinnen (p95 +33%) |
| ROM1 | 2.677 | 69,2% | +0,92% | +1,00 | +2.184 | Orchestrator liefert echten Mehrwert (+8pp WR, positiver ø) |
| TD_1H / TD_4H | 2.794 | 57,3% | ~+1,0% | ≈0 | +2.387 | ok; TD_1H ist zudem das am besten kalibrierte Modell (Step 2) |
| ATS1 | 1.768 | 65,8% | +1,02% | 0,00 | +1.622 | positiv trotz Trainer-Mängeln (Report 13) |
| MIS1-8H/24H | 1.003 | ~52% | +1,4% | negativ | +1.261 | kleine n, tail-getrieben |
| ABR1 | 110 | 63,6% | +3,15% | 0,00 | +335 | klein; Modell real nur 7 Features (Report 13) |
| SRA1 | 396 | 69,9% | +0,44% | +1,12 | +134 | gesund, klein |
| BB_4H | 2.162 | 61,2% | +0,36% | −0,05 | +565 | knapp positiv |
| QM_1H | 3.139 | 67,5% | +0,06% | −0,03 | **−139** | 67% WR und trotzdem ≈ 0 — TP1-Wins geben alles zurück |
| ATB1 | 306 | 65,7% | −0,46% | 0,00 | −172 | negativ (passt zu Report-13-Verdikt) |
| BR4H / BR2H / BR1H | 11.756 | 58–60% | −0,1…−0,3% | ≈0 | **−4.106** | ganze BR-Familie netto negativ; BR1H LONG 65,5% vs SHORT 49,5% WR |
| BB_1H | 3.909 | 55,7% | −0,18% | −0,17 | −1.089 | negativ |
| QM_4H | 556 | 54,9% | −0,40% | −0,29 | −277 | negativ |
| UFI1 | 35 | 25,7% | **−7,90%** | −3,22 | −280 | katastrophal (bestätigt P0.11) |
| AIM1 | 3.047 | 50,8% | **−1,02%** | −1,01 | **−3.399** | konsistent negativ — passt zum invertierten Modell (Report 13); Feb-Start mit 24% WR |

**Muster:**
- **WR ist irreführend.** Median-PnL ist bei fast allen Modellen ≈ 0 oder negativ — TP1-Berührung zählt als Win, aber der Trade endet oft per Trailing/SL nahe Einstand. Die Summen entstehen in den Tails (p95). Ein Modell mit 67% WR (QM_1H) ist netto negativ, eines mit 58% (MIS1-168H) klar positiv.
- **Richtungs-Asymmetrien sind groß:** EPD1 SHORT 76,5% vs LONG 50,2% WR; BR1H LONG 65,5% vs SHORT 49,5%; RUB1 SHORT 63,9% vs LONG 48,7%. Ein Direction-Gate pro Modell wäre ein billiger Sofort-Hebel.
- **Regime-Drift sichtbar:** BR-/BB-Familie war Mär–Apr stark negativ und ab Mai positiv (aber mit Mini-n, weil das Regime-Gating sie inzwischen fast wegfiltert); MIS1-168H seit Mai unter 50% WR. Monatsscheiben stehen im Anhang des Analyse-Skripts.
- **Leverage nicht eingerechnet:** Bots mit hohem Hebel (R4-Findings) verwandeln „−0,3% ø" in reale Kontoverluste; UFI1s −7,9% bei 20x wäre Liquidation.

## C. Classic-Strategien — dedupliziert, nur Rows mit Close-Preis (n=184.331)

Gesamt: **WR 62,7%, ø −0,07%/Trade, Summe −13.360 Preis-%** — die Classic-Familie ist in Summe ein Nullsummen- bis Verlustgeschäft, obwohl alle „Win-Raten" > 60% aussehen.

| Strategie | n | WR | ø PnL | Median | Σ netto | Anmerkung |
|---|---|---|---|---|---|---|
| Support Resistance | 1.917 | 63,5% | +0,41% | 0,00 | **+596** | einzige netto-positive; SHORT (+0,66% ø) trägt alles |
| Main Channel | 202 | 67,3% | −0,28% | 0,00 | −77 | klein, ≈ 0 |
| Volume Indicator | 51.440 | 64,1% | +0,09% | −0,10 | **−705** | brutto +4.439, Fees fressen es; Feb/Mai/Jun positiv, Mär/Apr −7,2k |
| 5 Percent | 19.385 | 71,1% | −0,20% | −0,05 | **−5.766** | 71% WR und klar negativ — Paradebeispiel Win≠Profit |
| Fast In And Out | 111.387 | 60,6% | −0,13% | **+1,25** | **−25.843** | Median positiv, ø negativ → seltene, große Verlust-Tails (p5 −2,7 täuscht; abs>50%-Ausreißer konzentriert hier) |

**Interpretation:** Die Classic-Strats produzieren enorme Signalmengen (FIFO 111k Trades!) mit winzigen Gewinnen pro Trade, die von Verlust-Tails und Fees aufgefressen werden. Bei FIFO ist der Median +1,25% (TP1-Scalps funktionieren), aber die Verlierer sind selten UND groß — klassisches „picking up pennies". Volume Indicator wäre mit besserem Exit/Fee-Management ≈ break-even. Der Zensur-Anteil (FORCE_CLOSED/DELISTED/REGIME) liegt bei 1–6% und verzerrt nach P1.9 zusätzlich optimistisch.

## D. Konsequenzen / Empfehlungen

**Datenhygiene (vor jeder weiteren Auswertung):**
1. Unique-Index `(symbol, model, direction, open_time)` auf `closed_ai_signals` + Purge der 357k Duplikat-/LEGACY-Rows (Backup vorher). Dito Classic (11k).
2. Alt-Namensvarianten (`MSI1-*`, `MIS1-*h_*`, `ATS1_Robust`) archivieren.

**Portfolio-Entscheidungen (auf Basis realisierter Zahlen + Report 13):**
3. **Stoppen/parken:** AIM1 (invertiert + −3,4k netto), UFI1 (25,7% WR, −7,9%/Trade), QM_4H, ATB1. Prüfen: BB_1H, BR1H/BR2H (netto negativ; ggf. nur LONG-Seite bei BR1H behalten).
4. **Behalten/fokussieren:** MIS1-72H, EPD1 (aber Juli-Knick beobachten + Report-13-Gate-Fix), MIS1-168H (Drift beobachten), ROM1/Orchestrator (echter Mehrwert — nach P0.4-Whitelist-Fix sollte er weiter steigen), TD_1H, ATS1, SRA1, Support Resistance.
5. **Direction-Gates:** EPD1 LONG aus, RUB1 LONG aus, BR1H SHORT aus, 5 Percent LONG prüfen (n=1.087 zu klein für 76% WR-Vertrauen).
6. Classic-Familie: Exits überarbeiten (Trailing-Give-back + Fees), sonst trägt nur Support Resistance sich selbst.
7. KPI-Definition ändern: statt „WR (TP1-Touch)" den **ø Netto-PnL/Trade und Median** als Dashboard-Hauptmetrik führen — die aktuelle WR-Anzeige belohnt genau das falsche Verhalten.

**Nächster Verifikationsschritt:** Exchange-/Cornix-Abgleich einer Stichprobe (z.B. 50 Trades quer durch Modelle) gegen die Monitor-PnL, um P1.2/P2.7-Verzerrung zu quantifizieren.
