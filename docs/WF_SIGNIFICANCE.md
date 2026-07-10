# WF-Signifikanz-Layer (`tools/wf_significance.py`)

**Zweck:** Ein Replay-Summary sagt „+38 R über 365d" — dieser Layer beantwortet
die Folgefrage, ob dieser Edge von Rauschen unterscheidbar ist, bevor ein
Kandidat Richtung Live-Gate diskutiert wird. Rein additiv über dem Output von
`tools/walkforward_sim.py`, kein Eingriff in den Simulator.
(T-2026-CU-9050-027 D3; Vorbild HKUDS/Vibe-Trading `backtest/validation.py` +
`bench_runner_strict.py`, MIT — adaptiert, kein Drop-in.)

## Aufruf

```
python tools/wf_significance.py <pfad>/{tag}_replay_{days}d.jsonl \
    [--group-by strategy|strategy+direction] [--n 1000] [--seed 42] \
    [--fee-per-side 0.05] [--min-trades 20] [--out report.json]
```

Input ist das Trade-JSONL des Walk-Forward-Simulators (Felder `strategy`,
`direction`, `signal_time`, `outcome_tp1`, `net_pnl_pct`, `r_multiple`).
Output: Konsolen-Report + `<input>_significance.json`. Deterministisch bei
fixem Seed (Default 42). Replay-Artefakte liegen auf dem VPS
(`Documents\_X\staging_models\replay`) — der Lauf über echte Batch-E-Outputs
ist eine VPS-Session; auf der Build-Maschine verifizieren die synthetischen
Tests (`backtest/test_wf_significance.py`).

## Die drei Statistiken

1. **Random-Control (Sign-Flip, der Kern).** H0: die Richtungswahl hat keinen
   Edge — jeder Trade ist austauschbar mit dem Gegen-Trade auf derselben
   Geometrie, der dieselben Fees zahlt (`flip(net) = -net - 2*fee_rt`).
   1000 zufällige Flip-Masken liefern die Null-Verteilung des Mittelwerts →
   `p_value` + `random_control_delta_pct`. Bewusst kein Test gegen 0: die
   Kontrolle trägt den Fee-Drag eines richtungslosen Zufalls-Traders.
2. **Reihenfolge-Permutation für den Max-Drawdown.** Prüft, ob die
   Verlust-Clusterung des beobachteten Pfads zufallstypisch ist
   (`p_value_dd_worse` = Anteil Permutationen mit tieferem DD). Der
   vt-Permutationstest auf Sharpe wurde bewusst NICHT übernommen: bei
   per-Trade-%-PnL ist Sharpe unter Reihenfolge-Permutation invariant — der
   Test wäre degeneriert. Der DD wird **absolut in %-Punkten** unter dem Peak
   gemessen (`equity − peak`), nicht auf die Peak-Höhe normiert — sonst
   konfundiert die zufällige Peak-Höhe der fleet-weiten Multi-Coin-Replays den
   Test (T-2026-CU-9050-053, siehe „Befund" unten). Es ist eine
   Pfad-Clusterungs-Statistik, kein echter Portfolio-Drawdown (gleichzeitige
   Signale bleiben sequenziell verkettet).
3. **Bootstrap-CIs** (Resampling mit Zurücklegen) für per-Trade-Sharpe
   (bewusst nicht annualisiert — Trades sind nicht zeit-regulär), `avg_r` und
   TP1-Win-Rate, je mit `prob_positive`.

## Lese-Hilfe

- `random_control.p_value < 0.05` UND `sharpe_per_trade_ci[0] > 0`: Edge ist
  von Zufall unterscheidbar — Kandidat für die nächste Batch-E-Stufe.
- `p_value_dd_worse` (absoluter DD in %-Punkten, seit T-2026-CU-9050-053):
  **klein** (≲ 0,05) = die Verluste clustern in der echten Chronologie
  untypisch **maligne** — kaum eine Zufallsreihenfolge ist so schlecht; die
  Regime-Abhängigkeit prüfen und das DD-Risiko am beobachteten Wert messen.
  **Nahe 1** = fast jede Reihenfolge wäre gleich schlimm oder schlimmer, der
  Pfad war untypisch gnädig → das DD-Budget aus `simulated_max_dd_median_pp`
  nehmen. Der Wert ist eine **Pfad-Clusterungs-Statistik in %-Punkten**, kein
  echter Portfolio-Drawdown (gleichzeitige Signale bleiben sequenziell
  verkettet — Grenze, siehe „Befund" unten). Bis zum Fix war diese Regel auf
  den Multi-Coin-Replays durch die Peak-Normierung genau verkehrt herum.
- **Grenzen:** testet EINEN Kandidaten. Wer viele Varianten screent, braucht
  zusätzlich FDR/Deflated-Sharpe (bewusst Non-Scope, eigener Task). Kein
  Ersatz für Purge/Embargo im Simulator selbst. Und: die Sign-Flip-Kontrolle
  nimmt `gross' = -gross` an — ein real reversierter Trade wäre bei
  SL-/TP-gekappten Ladder-Profilen früher gestoppt worden. Die Kontrolle ist
  dadurch bei Trend-Following-artigen R:R-Profilen zu negativ, **p-Werte eher
  zu klein**: knappe Signifikanz nicht überlesen als Beweis. Fairere Kontrolle
  (simulate_exit-Re-Run mit gespiegelter Richtung) = eigener Task.

## Erster Lauf über echte Batch-E-Outputs (2026-07-10, VPS)

T-2026-CU-9050-040. `--group-by strategy+direction`, `--n 1000`, `--seed 42`,
`--fee-per-side 0.05`; Inputs aus `Documents\_X\staging_models\replay`.
Lauf ist read-only und deterministisch reproduzierbar (identischer Report bei
Wiederholung). Interpreter: `py -3.13` — das PATH-`python` (3.14) hat kein numpy.

| Kandidat | n_closed | mean PnL % | Kontrolle % | p | Sharpe/Trade (95% CI) | avg_r | TP1-WR |
|---|---|---|---|---|---|---|---|
| mis1/LONG | 175.089 | −0,2601 | −0,1000 | 1,000 | [−0,0409, −0,0312] | −0,0463 | 55,9 % |
| mis1/SHORT | 175.027 | +0,0362 | −0,1001 | 0,001 | [+0,0006, +0,0097] | +0,0095 | 56,3 % |
| rub/LONG | 52.081 | −0,3246 | −0,1006 | 1,000 | [−0,0382, −0,0203] | −0,0128 | 60,6 % |
| rub/SHORT | 45.560 | −0,2528 | −0,0996 | 1,000 | [−0,0401, −0,0219] | −0,0269 | 73,9 % |
| abr1/LONG | 77.398 | −0,5480 | −0,0989 | 1,000 | [−0,1156, −0,1008] | −0,0890 | 55,7 % |
| abr1/SHORT | 91.627 | +0,2720 | −0,1002 | 0,001 | [+0,0391, +0,0519] | +0,0445 | 59,2 % |
| ufi1/SHORT | 384 | +17,6594 | −0,0961 | 0,001 | [+0,2726, +0,4867] | +0,3663 | 50,8 % |

**Der Layer verhält sich wie spezifiziert.** Zwei unabhängige Gegenproben:
das Kontroll-Mittel trifft in allen sieben Gruppen den Round-Trip-Fee-Drag
(−0,0961 … −0,1006 gegen erwartete −0,10), und die trade-gewichteten
Aggregate aus dem Report reproduzieren die `*_summary.json` des Simulators
exakt (mis1: WR 56,09 % / avg_r −0,0184 / avg_pnl −0,1120 gegen 56,1 /
−0,0184 / −0,112; rub analog). Der p-Wert stimmt in allen Gruppen mit dem
Vorzeichen des Sharpe-CI überein.

**Die Replays tragen die ROHEN Detector-Signale, vor dem Modell-Filter.** Die
Tabelle bewertet also den Detektor, nicht das deployte Modell — kein Deploy-
Argument in beide Richtungen:

- **abr1** deckt sich mit dem Live-Bild: SHORT hat einen Roh-Edge, LONG ist
  signifikant schlechter als ein richtungsloser Zufalls-Trader (SHORT läuft
  binary @0,75; LONG nur als funding-gated Experiment).
- **rub** ist roh in BEIDEN Richtungen negativ, obwohl RUB2-SHORT live
  deployed ist. Der Edge kommt dort aus der Modell-Selektion, nicht aus dem
  Detektor. Ein Signifikanz-Lauf über Roh-Signale kann ein gutes Modell also
  nicht widerlegen.
- **mis1/SHORT** ist trotz p = 0,001 praktisch ein Null-Edge (untere CI-Grenze
  0,0006, avg_r +0,0095). Dazu biast die Sign-Flip-Kontrolle p nach unten —
  genau der Fall, vor dem die Grenzen-Notiz warnt.
- **ufi1/SHORT** ist der einzige große Roh-Edge, steht aber auf n = 384,
  SHORT-only und einem Zeitfenster. Kein Anlass, den Park-Entscheid
  anzufassen.

## Befund (behoben, T-2026-CU-9050-053): Peak-Normierung konfundierte Statistik 2

**Diagnose (Stand des Erst-Laufs).** `max_drawdown_pct` normierte den Drawdown
auf den laufenden Peak (`(equity − peak) / peak`). Auf diesen Replays trägt die
additive Equity (`100 + Σ %-PnL`) das nicht: pro Zeitstempel liegen 8,8 (rub)
bis 20,2 (mis1) gleichzeitige Signale über 530–648 Coins an, die der Pfad als
sequenzielle Einzelwetten verkettet. Die Equity fällt dadurch tief unter null
(rub/LONG: 72 % des Pfades negativ, Tief −35.072) und der Quotient misst am Ende
vor allem, **wie hoch der Peak zufällig stand**: mis1/SHORT und abr1/SHORT haben
ihren Peak bei Trade 0 (≈ 95), rub/LONG bei 2.477 — daher dort ein optisch
mildes −421 % gegen einen Permutations-Median von −7.203 %. Nebenbefund: der
Guard `np.where(peak > 0, peak, 1.0)` wechselte bei Peak ≤ 0 stillschweigend
Einheit UND Skalierung (relativ → %-Punkte × 100).

**Fix.** `max_drawdown_pct` rechnet den DD jetzt **absolut in %-Punkten** unter
dem Peak (`equity − peak`, ohne Normierung; die +100-Basis kürzt sich heraus).
Beobachteter und permutierter Pfad werden damit exakt gleich gemessen, frei vom
Peak-Höhen-Artefakt; der Guard entfällt ersatzlos, weil keine Division mehr
stattfindet. Damit ist `p_value_dd_worse` wieder operativ lesbar (Lese-Hilfe
oben). Gewählte Option: absoluter DD statt eines overlap-respektierenden
Equity-Pfads — letzterer bräuchte Kapitalallokations-/Sizing-Annahmen, die das
Replay-JSONL nicht trägt, und würde von der `sum_net_pnl_pct`-Reporting-Konvention
abweichen. **Grenze:** die Zahl bleibt eine Pfad-Clusterungs-Statistik, kein
echter Portfolio-Drawdown (Overlap sequenziell verkettet).

Die operative Aussage kippt (200 Permutationen, Seed 42, `--fee-per-side 0,05`;
mit dem gefixten Tool reproduziert):

| Kandidat | p_dd_worse vor Fix (relativ) | p_dd_worse nach Fix (absolut, Tool) |
|---|---|---|
| rub/LONG | 1,000 („untypisch gnädig") | 0,005 (maligne Clusterung; beob. −55.208 vs Median −17.182) |
| abr1/SHORT | 0,005 | 0,005 |
| ufi1/SHORT | 0,035 | 0,005 (beob. −1.436,72 vs Median −278,19) |

Für rub/LONG hätte die alte Lese-Regel das DD-Budget aus
`simulated_max_dd_median_pp` genommen, obwohl der beobachtete Pfad schlechter
war als 199 von 200 Zufallsreihenfolgen — jetzt zeigt der Test das korrekt an.

**Keine Deploy-Aussage der obigen Batch-E-Tabelle ändert sich.** Sie steht auf
Statistik 1 (Random-Control `p`) und 3 (Sharpe-CI), beide reihenfolge-invariant
und vom DD-Fix unberührt; die Werte oben wurden mit dem gefixten Tool identisch
reproduziert (rub/LONG mean −0,3246, `p`=1,000, Sharpe negativ). Die
Drawdown-Statistik war ohnehin als „nicht operativ lesen" markiert und ging in
keinen Deploy-Call ein — sie ist jetzt nur wieder benutzbar.
