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
   Test wäre degeneriert.
3. **Bootstrap-CIs** (Resampling mit Zurücklegen) für per-Trade-Sharpe
   (bewusst nicht annualisiert — Trades sind nicht zeit-regulär), `avg_r` und
   TP1-Win-Rate, je mit `prob_positive`.

## Lese-Hilfe

- `random_control.p_value < 0.05` UND `sharpe_per_trade_ci[0] > 0`: Edge ist
  von Zufall unterscheidbar — Kandidat für die nächste Batch-E-Stufe.
- `p_value_dd_worse` **klein**: kaum eine zufällige Reihenfolge derselben
  Trades wäre so schlimm — die Verluste clustern untypisch maligne in der
  echten Chronologie (Tail-Risk-/Regime-Signal, genau der Fall, den der Test
  fangen soll). `p_value_dd_worse` **nahe 1**: der beobachtete Pfad war
  untypisch gnädig — das DD-Budget aus `simulated_max_dd_median_pct` nehmen,
  nicht aus dem beobachteten MaxDD.
- **Grenzen:** testet EINEN Kandidaten. Wer viele Varianten screent, braucht
  zusätzlich FDR/Deflated-Sharpe (bewusst Non-Scope, eigener Task). Kein
  Ersatz für Purge/Embargo im Simulator selbst. Und: die Sign-Flip-Kontrolle
  nimmt `gross' = -gross` an — ein real reversierter Trade wäre bei
  SL-/TP-gekappten Ladder-Profilen früher gestoppt worden. Die Kontrolle ist
  dadurch bei Trend-Following-artigen R:R-Profilen zu negativ, **p-Werte eher
  zu klein**: knappe Signifikanz nicht überlesen als Beweis. Fairere Kontrolle
  (simulate_exit-Re-Run mit gespiegelter Richtung) = eigener Task.
