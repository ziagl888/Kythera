# whitelist_v2 nachjustieren: Verdikt NO-GO (T-2026-KYT-9050-007)

**Auftrag (Michi, 2026-08-02):** v2 nicht flippen und nicht so lassen, sondern
**nachjustieren** — Wilson-Grenze und Break-even-Schwelle ändern und gegen die
realisierten Forwards neu messen. Stop-B des Tickets gilt: zeigt v2 keinen
messbaren Mehrwert, bleibt v1.

**Ergebnis: Stop-B greift. Keine Parametrisierung von v2 überlebt
out-of-sample.** Der Gate bleibt auf v1. Kein Flip, kein Restart, keine
Schreib-Query.

Werkzeug: `tools/whitelist_v2_recalibration.py` (read-only). Läufe:
`staging_models/replay/whitelist_v2_recalibration_2026-07-11.md` (in-sample) und
`…_oos_pre-2026-07-03.md` (out-of-sample).

---

## 1. Der vorgeschlagene Hebel ist der falsche

Über 45 Konfigurationen (z × k × break-even) auf 1.590 Zellen:

| Hebel | Bewegung der Öffnungsrate |
|---|---|
| Break-even 0,1 → −0,1 | +1,7 pp |
| Shrinkage k 25 → 5 | +1,3 pp |
| **z 1,64 → 0,67** | **+10,0 pp** |
| **z 1,64 → 0** | **+29 bis +47 pp** |

Die Strenge von v2 steckt fast vollständig im **z-Multiplikator der
Untergrenze**. Break-even und Shrinkage bewegen den Gate um ein bis zwei
Prozentpunkte — beides sind die Stellschrauben, die der Auftrag nennt, und beide
sind wirkungslos.

Zweiter Befund derselben Tabelle: **selbst am permissivsten Ende öffnet v2 nur
~53 % der Zellen gegen v1s 94 %.** Der gemessene −55-%-Durchsatzverlust aus PR
#239 ist kein Tuning-Artefakt, sondern strukturell.

## 2. In-sample sah eine Region gut aus

Fenster 2026-07-11 → 08-02, 8.367 geforwardete Events mit ROM1-Bein,
v1-Referenz **Ø +0,0329 %/Trade** (Σ +108,1 %).

| Konfiguration | Durchlass | Ø behalten | Ø geblockt | Lesart |
|---|---:|---:|---:|---|
| z 1,64 / k 25 / be 0,1 **(heute)** | 6,8 % | +0,212 | +0,013 | entfernt Gewinner |
| z 0,67 / k 10 / be 0,1 | 13,9 % | **+0,558** | **−0,076** | entfernt Verlierer |
| z 0,00 / k 25 / be 0,1 | 25,5 % | +0,396 | −0,128 | entfernt Verlierer |

Gelesen als Backtest wäre das ein Treffer: doppelter Durchsatz, dreifache
behaltene Erwartung, und das Entfernte war negativ. Genau deshalb steht es hier
nur als Zwischenschritt.

## 3. Out-of-sample invertiert es — vollständig

Fenster 2026-04-18 → 07-03 (endet **vor** dem 30-Tage-Fit-Fenster der
Zellstatistiken), 4.356 geforwardete Events mit ROM1-Bein, 99,9 % Leg-Abdeckung,
v1-Referenz **Ø +0,6886 %/Trade** (Σ +1.899,1 %).

| Konfiguration | Durchlass | Ø behalten | Ø geblockt | Σ geblockt | Lesart |
|---|---:|---:|---:|---:|---|
| z 1,64 / k 25 / be 0,1 (heute) | 3,4 % | +4,369 | **+0,554** | **+1.475,3** | entfernt GEWINNER |
| z 0,67 / k 10 / be 0,1 | 5,3 % | +2,328 | **+0,594** | **+1.547,6** | entfernt GEWINNER |
| z 0,00 / k 25 / be 0,1 | 5,6 % | +2,333 | +0,589 | +1.530,5 | entfernt GEWINNER |

**42 der 45 Konfigurationen entfernen Gewinner.** Über das ganze Gitter liegt der
Mittelwert der geblockten Beine bei **+0,55 bis +0,60 %/Trade** — v2 hätte in
jeder Parametrisierung rund **80 % des realisierten ROM1-Gewinns** dieses
Fensters weggeschnitten und dafür 3–6 % des Volumens behalten.

Die drei Ausnahmen (`z 0 / be −0,1`) behalten **95 %** des Verkehrs: ein Gate,
das praktisch nicht gatet, mit einem geblockten Σ von −8 bis −24 % — Rauschen.

**Die in-sample gefundene Region kehrt sich um.** `z 0,67 / be 0,1` geht von
„entfernt Verlierer" (Ø geblockt −0,076) zu „entfernt GEWINNER" (Ø geblockt
+0,594). Das ist kein schwächeres Ergebnis, es ist das entgegengesetzte.

## 4. Warum in-sample so gut aussah

Selektionseffekt, und zwar der stärkste denkbare. Die Zellstatistiken stammen
aus den letzten 30 Tagen; die in-sample gescorten Beine liegen **in genau diesem
Fenster**. Eine Zelle passiert den Gate, weil ihre jüngsten Trades gut liefen —
und dieselben Trades werden dann als Beleg gezählt. Je weiter man z öffnet,
desto mehr solcher selbstbestätigenden Zellen kommen dazu, und die behaltene
Erwartung steigt scheinbar an.

Das ist dieselbe Klasse Fehler, die PR #239 auf dem Trigger-Leg benannt hat, hier
nur über den Umweg der Parametrisierung. Der out-of-sample-Lauf ist die
Gegenprobe, und er fällt eindeutig aus.

**Neu gegenüber PR #239:** dieser Out-of-Sample-Lauf war dort nicht möglich.
Die Flip-Auswertung braucht `orchestrator_open_trades.wl_reason`, das erst ab
Anfang Juli befüllt ist — vor dem Fit-Fenster gab es null verwertbare Events.
Dieses Werkzeug entscheidet die Zelle **neu** aus `bot_regime_performance` und
braucht `wl_reason` nicht; damit werden die 4.359 Forwards von April bis Anfang
Juli auswertbar. Die „null Out-of-Sample"-Lücke des Ursprungsberichts ist
geschlossen — mit einem negativen Befund.

## 5. Was das Ergebnis NICHT ist

Kein Backtest. `bot_regime_performance` ist ein **Snapshot** — auf jedem Lauf
gemessen und im Bericht ausgewiesen: **0 Zellen** mit mehr als einer Zeile. Die
Statistiken, auf denen der Gate damals entschieden hat, existieren nicht mehr.
Beide Läufe benutzen **heutige** Zellstatistiken; sie unterscheiden sich darin,
ob die gescorten Trades im Fit-Fenster liegen oder nicht. Für einen
Leakage-Test ist genau das die richtige Trennung — für eine
Rollout-Rechtfertigung reicht es nicht.

Zweite Einschränkung: die beiden Fenster haben sehr verschiedene Basisniveaus
(Ø +0,033 gegen +0,689 %/Trade). Sie stammen aus unterschiedlichen
Marktphasen; die absoluten Beträge sind nicht direkt vergleichbar. Das
**Vorzeichen** der geblockten Seite ist es.

Dritte: nur die geforwardete Seite ist gescort. Unterdrückte Signale haben per
Konstruktion kein ROM1-Bein — ihr Ausgang ist nicht beobachtet, nicht null.

## 6. Empfehlung

1. **v1 bleibt.** Der Gate wird nicht geflippt und nicht nachjustiert. Stop-B
   ist erfüllt: v2 zeigt keinen messbaren Mehrwert, in keiner Parametrisierung.
2. **Break-even und Shrinkage nicht weiter anfassen** — als Hebel erwiesen sich
   beide als wirkungslos, und ein Absenken des Break-even ist über beide Fenster
   hinweg schädlich.
3. **`bot_regime_performance` historisieren** (eine Snapshot-Zeile pro Tag).
   Solange die Zellstatistiken überschrieben werden, kann keine
   Gate-Variante je sauber gegen ihre eigene Vergangenheit geprüft werden — der
   Leakage-Test oben ist die beste erreichbare Näherung, nicht der richtige Test.
   Erfasst als Folge-Task.
4. Falls v2 später doch verfolgt wird: **nur über ein Live-Shadow-A/B**, wie
   T-031 es für das SOFT-Regime-Gate festgehalten hat.
