# T-2026-KYT-9050-008 — RUB2 Replay↔Live-Feature-Skew: Root-Cause

**Stand:** 2026-08-01 · **Session:** VPS (SRV02), read-only auf allen Live-Tabellen ·
**Werkzeug:** `tools/rub2_replay_skew_probe.py` (Report: `staging_models/rub2_replay_skew_probe.{json,md}`)

## Auftrag und Ergebnis in einem Absatz

Zu klären war der Befund aus T-2026-CU-9050-070: für dieselben (Symbol, Kerze)-Signale
korrelierten Live-Confidence (`ml_predictions_master`, RUB2-SHORT) und Replay-Prob
**−0,37** über 49 Paare — gleiches Modell, gleiche Kerze, also Feature-Skew. Die Hypothese
im Ticket waren die Funding-Features.

**Die Hypothese ist widerlegt.** Die Funding-Features sind nicht schief: alle sechs lassen
sich für **alle 229** gematchten Signale **bit-exakt** aus der heutigen `funding_rates`
rekonstruieren (mean|Δ| = 0,0 in jeder der sechs Spalten). Die −0,37 sind ein Artefakt des
Messfensters: der Überlappungszeitraum 06./07.07. liegt auf dem **Go-Live-Tag von
RUB2-SHORT**. Am 06.07. trug das Tag `RUB2` noch ein anderes Modell. Ab dem Umschaltpunkt
stimmen Live und Replay überein, ab dem 12.07. auf 92–100 % der Zeilen **exakt**.

## 1. Wie gemessen wurde

Fenster 2026-07-01 → 2026-08-01, RUB2-SHORT. 229 gematchte (Symbol, Kerze)-Paare über 128
Coins — das 4,7-fache der 49 Paare, auf denen der Ursprungsbefund beruhte.

Zwei methodische Punkte, weil beide das Vorzeichen des Ergebnisses drehen können:

- **Join-Schlüssel statt Offset.** `ml_predictions_master.time` ist `TIMESTAMP WITHOUT TIME
  ZONE`, beschrieben mit einem aware-UTC-`datetime` → Postgres hat beim Schreiben in die
  Session-Zone gecastet. Der Probe rechnet mit `AT TIME ZONE current_setting('TimeZone')`
  exakt diesen Cast zurück, statt die im 070-Report beobachteten −3 h fest zu verdrahten.
  Der Replay stempelt `signal_time` = Kerzen-Open + 1 h, der Bot scannt zu hh:10 und ankert
  auf hh:00 — der Schlüssel ist damit dieselbe Stunde auf beiden Seiten.
- **Zwei Artefakte, nicht eines.** `rub2_model_SHORT.pkl` im Repo-Root ist **nicht** das
  Modell, das im Juli-Fenster lief (siehe §3). Der Probe scored beide Kandidaten und weist
  je Tag aus, welcher die Live-Confidence reproduziert.

## 2. Root-Cause: das Messfenster lag auf dem Modellwechsel

| Tag | n | Pearson | mean\|Δ\| | Zeilen exakt |
|---|---|---|---|---|
| 2026-07-06 | 26 | **−0,45** | 0,161 | 0 % |
| 2026-07-07 | 19 | +0,60 | 0,030 | 0 % |
| 2026-07-08 | 21 | +0,98 | 0,015 | 0 % |
| 2026-07-09 | 38 | +0,98 | 0,011 | 0 % |
| 2026-07-10 | 59 | +0,97 | 0,009 | 3 % |
| 2026-07-11 | 38 | +0,97 | 0,014 | 0 % |
| 2026-07-12 | 25 | **+1,00** | 0,0003 | **92 %** |
| 2026-07-13 | 3 | **+1,00** | 0,000 | **100 %** |

Die −0,37 des Ursprungsberichts sind der Mittelwert über genau die beiden oberen Zeilen.
RUB2-SHORT wurde mit `07c8874` am 07.07. deployt; die Live-Zeilen davor stammen aus dem
alten 9-Feature-Legacy-Pfad, der unter demselben Tag `RUB2` postete. Der Umschaltpunkt ist
in den Daten sichtbar: **2026-07-07 ~07:00 UTC** (= 10:00 lokal, Commit 09:40 + Fleet-Neustart).
Davor reproduziert keines der beiden RUB2-Artefakte die Live-Werte, danach das
Zeitfenster-korrekte auf mean|Δ| 0,010.

Ein gepooltes Korrelationsmaß über eine Generationsgrenze hinweg misst den Generationswechsel,
nicht den Feature-Skew. Das ist die eigentliche Lehre des Befunds.

## 3. Zwei Voraussetzungen des Tickets waren zum Bearbeitungszeitpunkt bereits überholt

- **Der Replay ist längst neu erzeugt.** `rub_replay_365d.jsonl` trägt Erzeugungszeit
  **2026-07-14 10:47–11:52** — nach dem Funding-Backfill (11.07.) und nach den beiden
  Look-ahead-Fixes in `walkforward_sim` (`ac49bc3` Forming-Candle, `21a97a6` bfill-aus-der-Zukunft,
  beide 10.07.). Schritt 2 des Auftrags („Replay neu erzeugen") war damit erledigt, bevor
  diese Session begann — **kein Sim-Lauf gefahren** (Sequential-Jobs-Regel eingehalten).
- **Das Live-Artefakt hat mitgewechselt.** Aus demselben Lauf entstand ein RUB2-Retrain
  (`_X/staging_models/rub2_model_SHORT.pkl`, 14.07. 11:52, Threshold **0,7929**, n_test 1844),
  der in den Repo-Root promotet und mit `14e1c6f` (20.07.) in git nachgeführt wurde. Das
  Juli-Modell (Threshold **0,829**, n_test 4725) ist nur noch als
  `staging_models/max1_model_SHORT.pkl` erhalten — der byte-gleiche MAX1-Klon vom 11.07. Der
  Probe nimmt deshalb per Fit das passende Artefakt, nicht das mit dem passenden Dateinamen.

## 4. Der Rest-Unterschied 07.–11.07. — und warum er am 12.07. verschwindet

Nach dem Umschaltpunkt bleiben mean|Δ| 0,009–0,015, um am 12.07. auf 0,0003 zu fallen. Der
Sprung ist datierbar: **`logs/rsi_rewrite_execute_20260712.log` zeigt einen ausgeführten
Wilder-RSI-Rewrite der Indikator-Historie am 12.07. (11:26–21:02, „3831 tables, 88 426 142
cells written")** — Schritt (2) der P2.12-Sequenz. Damit gilt für das Fenster 07.–11.07.:
der Bot las beim Scoring den alten span-RSI, der am 14.07. erzeugte Replay liest für dieselben
Kerzen den überschriebenen Wilder-RSI. Ab dem 12.07. lesen beide Seiten dieselbe Domäne — und
die Übereinstimmung wird exakt.

Das ist genau das in P2.12 vorhergesagte **Mixed-History-Risiko**, hier zum ersten Mal
gemessen: **≈1 Prozentpunkt Wahrscheinlichkeit** auf RUB2-SHORT. Unabhängige Bestätigung: die
heute gespeicherten `rsi_14` stimmen über 8 Coins × Juli bit-genau mit einer
Wilder-Rekursion überein, nicht mit der alten `ewm(span)`-Formel.

Der Funding-Backfill vom 11.07. liegt im selben Fenster und kann denselben Effekt tragen — die
beiden lassen sich aus heutiger Sicht nicht sauber trennen, weil `funding_rates` keine
Insert-Zeit führt. Eine Obergrenze gibt es aber: setzt man den **kompletten** Funding-Block auf
seinen Live-Fallback (0, was `funding_features_asof` bei fehlender Historie liefert), bewegt
sich die Wahrscheinlichkeit im Mittel um 0,039 — der Rest-Unterschied liegt bei 76 % der Zeilen
darunter, bei 55 von 229 darüber. Funding allein erklärt ihn also **nicht** vollständig.

## 5. Feature-für-Feature: Replay-Datei gegen heutige DB

Alle 15 Modell-Inputs, aus der heutigen DB mit den geteilten Buildern nachgebaut:

| Gruppe | Ergebnis |
|---|---|
| `fund_last`, `fund_24h`, `fund_72h`, `fund_7d_cum`, `fund_pctl_90d`, `fund_trend` | **100 % identisch, mean\|Δ\| = 0** |
| `dist_to_trend`, `slope_trend` | **100 % identisch** auf dem Replay-Fenster |
| `rsi`, `TSI_Line`, `TSI_Signal`, `MACD_*`, `atr_pct`, `dist_ema200` | identisch bis auf float32-Speicherrundung (mean\|Δ\| ≤ 1,6e-6) |

Zusätzlich geprüft, weil es **nicht** dasselbe Fenster ist: der Replay regressiert über die
letzten `95·24` **Zeilen**, der Bot über alle geschlossenen Zeilen in einem 95-**Tage**-Fenster.
Auf einem Coin mit Kerzenlücken sind das verschiedene Fenster. Gemessener Effekt:
`dist_to_trend` mean|Δ| 3,3e-4, `slope_trend` 6,5e-6 — **kein** messbarer Effekt auf die
Wahrscheinlichkeit (alle Substitutions-Varianten scoren identisch). Der Unterschied ist real,
aber irrelevant; er wird hier festgehalten, damit die nächste Session ihn nicht erneut sucht.

## 6. Schritt 3 — Retrain-Ökonomie und MAX1-Kalibrierung

Der 070-Report schloss: „In 59 Tagen Replay-OOS erreicht KEIN Event 0,93 (p99 = 0,879), während
live 0,93+ mit ~1,1 Posts/Tag vorkommt" — daraus die Empfehlung `MAX1_MIN_PROB = 0,93`.

**Die zweite Hälfte dieses Satzes stammt aus denselben kontaminierten Zeilen.** Die
Live-Confidence-Verteilung je Modell-Generation:

| Generation | n | Ø | p99 | max | ≥ 0,93 |
|---|---|---|---|---|---|
| vor 07.07. 07:00 (Legacy unter Tag RUB2) | 128 | 0,622 | 0,968 | 0,983 | 5 |
| RUB2 @0,829 (07.–14.07.) | 790 | 0,754 | 0,865 | **0,876** | **0** |
| RUB2-Retrain @0,7929 (ab 14.07.) | 753 | 0,748 | 0,892 | **0,920** | **0** |

**RUB2-SHORT hat live nie 0,93 erreicht** — die fünf Zeilen darüber sind ausnahmslos
Legacy-Zeilen von vor dem Deploy. Damit stimmen Replay und Live auch in der Verteilung überein:
die Test-Slice des sauberen Replays (n = 1844, exakt der Split der Retrain-Meta) liegt bei
p99 0,841 / max 0,874 gegen live p99 0,865 / max 0,876. **Die Replay-Kurve ist für die
Kalibrierung wieder verwendbar** — der Grund, aus dem T-070 sie verworfen hat, existiert nicht.

Threshold-Kurve auf der Test-Slice (62,3 d, mit dem Juli-Artefakt gescored):

| Threshold | n | /Tag | WR % | Ø PnL % |
|---|---|---|---|---|
| 0,829 | 44 | 0,71 | 93,2 | +2,76 |
| 0,85 | 11 | 0,18 | 100,0 | +3,53 |
| ≥ 0,88 | 0 | — | — | — |

### Ist-Stand MAX1 (am 2026-08-01 selbst nachgesehen, nicht aus Doku übernommen)

Aus `C:\Users\Michael\Documents\Kythera\.env`: `MAX1_LIVE_POSTING=1`,
`MAX1_MIN_PROB=0.829`, `MAX1_MAX_PER_DAY=100000`. In `ml_predictions_master`: 308
MAX1-SHORT-Zeilen vom 11.07. bis 01.08., alle 308 `posted=true`, maximale Confidence 0,9199.

Zwei Konsequenzen, beide **Operator-Entscheid** (§6 OPUS-HANDOFF), hier nur ausgewiesen:

1. Der dokumentierte Default `MAX1_MIN_PROB = 0,93` hätte in 21 Tagen **null** Posts erzeugt.
   Die Empfehlung war auf der kontaminierten Kurve gebaut; der Operator hat sie ohnehin
   überstimmt.
2. Mit Floor 0,829 und Kappe 100000 ist der Throttle faktisch abgeschaltet — MAX1 postet
   praktisch jeden RUB2-SHORT-Kandidaten. Das ist ein anderes Regime als das in
   `docs/MODEL_INTENT.md` §8 notierte „0,85 + Kappe 3". Ob das so gewollt ist, entscheidet
   Michi; die Replay-Kurve oben ist die Grundlage, die jetzt wieder trägt.

## 7. Gefundener Latent-Defekt (behoben)

`tools/walkforward_sim.py` baute die Epoch-Achse der RUB-Regression mit
`open_time.astype("int64") / 1e9`. Das ist keine Einheitenumrechnung, sondern eine Wette auf die
**Auflösung** der Spalte: `astype` liefert die Zähleinheit des dtype. Unter der Fleet-Umgebung
(pandas 2.3.2 → `datetime64[ns]`) stimmt es; unter pandas ≥ 3.0 (`datetime64[us]`) schrumpft die
Achse um Faktor 1000 → `slope_trend`, einer der 15 Modell-Inputs, kommt **1000× zu groß** heraus,
während `dist_to_trend` daneben weiter passt (der Fit am Fensterende bleibt stabil). Genau so
ist es dieser Session beim ersten Rekonstruktionslauf passiert — Faktor exakt 1000,0 auf allen
229 Events —, und genau so hätte es die nächste Replay-Erzeugung auf einem neueren Interpreter
getroffen: ein Train/Serve-Skew, den kein Feature-Contract sieht, weil die Spalte da und endlich ist.

Behoben mit `core.time.epoch_seconds()` (normalisiert auf ns, bevor geteilt wird), eingesetzt in
`walkforward_sim` und den drei Studien-Tools mit demselben Muster. Unter dem Fleet-Interpreter
**byte-gleich** zum Vorzustand — verifiziert, keine stille Verhaltensänderung. Gepinnt in
`backtest/test_epoch_seconds.py` (mutations-geprüft: die alte Formel lässt 2 von 4 Tests fallen).

## 8. Bewusst NICHT gemacht

Kein Replay-Lauf (bereits am 14.07. erzeugt — ein neuer Lauf hätte nichts geklärt), kein Retrain,
keine Promotion, kein Gate-Flip, kein Restart, keine Schreib-Query gegen Live-Tabellen.
`core/funding_features.py` bleibt **unangetastet** — der Root-Cause sitzt nicht dort, und eine
Änderung wäre eine Live-Verhaltensänderung ohne Anlass gewesen.

## 9. Offen

- Die Trennung zwischen RSI-Rewrite und Funding-Backfill im Fenster 07.–11.07. ist nicht
  auflösbar (`funding_rates` hat keine Insert-Zeit). Größenordnung geklärt (≈1 pp), Zuordnung nicht.
- Ob der 14.07.-Retrain nach dem RSI-Rewrite vom 12.07. erneut gefahren werden sollte, ist die
  offene P2.12-Frage Schritt (3) — der Replay vom 14.07. liegt bereits **nach** dem Rewrite, der
  Retrain darauf also auf einheitlicher RSI-Domäne. Der ältere Teil der Replay-Historie ist durch
  den Rewrite ebenfalls eindomänig. Kein Handlungsbedarf erkennbar, aber nicht formal geprüft.
