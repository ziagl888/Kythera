# whitelist_v2-Flip — Entscheidungsgrundlage für Michi (T-2026-KYT-9050-007)

**Stand:** 2026-08-01/02 · **Messung:** `tools/whitelist_v2_realized_eval.py` gegen die Live-DB, strikt read-only · **Rohberichte:** `staging_models/replay/whitelist_v2_realized_eval_*.md` + `*_summary.json`

**Der Flip ist nicht gemacht und wird hier nicht empfohlen oder abgelehnt** — `28_signal_orchestrator.get_whitelist_decision` liest unverändert `whitelisted` (v1). Dieses Dokument liefert die Zahlen, an denen der Entscheid hängt, und benennt, was die Zahlen **nicht** hergeben.

---

## 0. Die Kurzfassung in fünf Sätzen

1. Der Flip ist keine Feinjustierung: er blockt **87,7 % aller Whitelist-Zellen** und schneidet den ROM1-Durchsatz von **377 auf 168 Forwards/Tag (−55 %)**.
2. Auf dem Bein, das der Gate zum Entscheiden benutzt (Trigger-Bot), sieht v2 gut aus — **aber genau dieses Bein ist das, worauf v2 gefittet wurde**; das ist kein unabhängiger Beleg.
3. Auf dem Bein, das das Geld trägt (ROM1), ist der Effekt **≈ null und im Vorzeichen instabil**: die Signale, die v2 zusätzlich blocken würde, haben als ROM1 über 21 Tage **+2,0 %** realisiert (1.342 entschiedene Trades) und über die letzten 7 Tage **−61,6 %**.
4. Die Seite „v2 schaltet zusätzlich frei" hängt an **3 Zellen** und faktisch **einem Bein (AIM2-SHORT)** — und ist in ROM1-Geld **prinzipiell nicht messbar**, weil diese Signale nie gehandelt wurden.
5. **Out-of-sample gibt es keinen einzigen belastbaren Datenpunkt** (§5) — nicht wenig, sondern keinen. Wer den Flip auf Evidenz stützen will, braucht erst eine Messung, die es heute nicht gibt (§7).

---

## 1. Was der Flip mechanisch ändert

`get_whitelist_decision` tauscht genau einen Spaltenlesevorgang: `SELECT whitelisted` → `SELECT whitelisted_v2`. Alle anderen Gate-Pfade (`no_whitelist_entry`, `whitelist_stale:*`, `regime_is_transition:*`, `regime_unstable:*`) sind identisch — sie laufen über `is_whitelisted_fallback` und kennen die 4D-Zelle nicht.

**Zell-Matrix, Snapshot 2026-08-01 22:08 UTC (1.590 Zellen, v2-Coverage 100 %, Analyzer frisch, Alter 0,3 h):**

| | v2 pass | v2 block | Σ |
|---|---:|---:|---:|
| **v1 open** | 94 | **1.395** | 1.489 |
| **v1 block** | **3** | 98 | 101 |

- v1 offen: **93,6 %** der Zellen · v2 offen: **6,1 %**.
- Die Prämisse „die Whitelist ist zu ~89 % default-open" ist **bestätigt**: 1.410 von 1.590 Zellen (**88,7 %**) tragen `insufficient_data`, also v1s Default-Open-Krücke (n < 30).
- Von den 1.395 Zellen, die v2 zusätzlich blockt, sind **1.335 genau diese Krücken-Zellen** und **60 v1-Entscheidungen auf Merit** (`wr_above_overall`/`counter_trend_specialist`).
- Die **drei** Zellen, die v2 zusätzlich öffnen würde, namentlich:

| Bot | Regime | Alt | Dir | v1-Grund | v2-Grund |
|---|---|---|---|---|---|
| AIM2 | TREND_UP | ALT_NEUTRAL | SHORT | counter_trend_insufficient | `v2_pass:lb=0.912:est=2.515:src=cell:neff=124` |
| QM_4H | HIGH_VOLA | ALT_WEAK | LONG | wr_below_overall | `v2_pass:lb=1.143:est=2.432:src=cell:neff=117` |
| SRA2 | CHOP | ALT_NEUTRAL | SHORT | wr_below_overall | `v2_pass:lb=0.488:est=1.131:src=cell:neff=163` |

---

## 2. Wieviele echte Signale betrifft das? (Fenster A: 2026-07-11 → 2026-08-01)

22.660 aufgezeichnete Gate-Events, davon **14.234 zell-entschieden** (der Rest läuft über Fallback-Pfade, die der Flip nicht anfasst).

| | Events |
|---|---:|
| v2 würde **zusätzlich blocken** (`v2_would_block`) | **4.848** |
| v2 würde **zusätzlich durchlassen** (`v2_would_open`) | **264** |
| beide offen | 316 |
| beide geblockt | 8.806 |

- Gate-Rate offen auf zell-entschiedenem Traffic: **36,28 % → 4,07 %**.
- ROM1-Forwards/Tag inkl. des unveränderten Fallback-Sockels: **377,0 → 168,1 (−55 %)**.

**Korrektur einer naheliegenden Fehllesung.** Aus „89 % der Zellen sind default-open" folgt *nicht*, dass v2 vor allem die Krücke entfernt. Auf dem **Traffic** ist es umgekehrt:

| v1-Pfad der zusätzlich geblockten Events | Events | Anteil |
|---|---:|---:|
| `wr_above_overall` (Entscheidung auf **Merit**) | 3.964 | **81,8 %** |
| `insufficient_data` (Default-Open-**Krücke**) | 880 | 18,2 % |
| `counter_trend_specialist` | 4 | 0,1 % |

Die Krücken-Zellen sind zahlreich, tragen aber kaum Verkehr. **Der Flip überstimmt überwiegend Entscheidungen, die v1 auf Datenbasis getroffen hat** — er räumt nicht bloß leere Zellen auf.

---

## 3. Was haben genau diese Signale realisiert?

Zwei Messlatten, bewusst getrennt (Details: `docs/WHITELIST_V2_REALIZED_EVAL.md`):

- **Trigger-Leg** — der eigene, vom Monitor gescorte Trade des Quell-Bots. Existiert auf **beiden** Gate-Seiten (ein geblocktes Signal lief trotzdem im eigenen Channel des Bots) → die einzige symmetrische Messung.
- **ROM1-Leg** — der Trade, den der Orchestrator tatsächlich eröffnet hat. **Das echte Geld**, aber nur auf der geforwardeten Seite.

Ausgewiesen wird die **saubere Teilmenge** (`v1_agree`: die heutige v1-Zelle passt noch zur aufgezeichneten Gate-Entscheidung). Wo sie nicht passt, hat sich die Zelle seither bewegt, und die „Divergenz" vergleicht zwei v1-Stände statt v1 gegen v2 — diese Events stehen daneben, nicht drin.

### Fenster A (2026-07-11 → 2026-08-01, 21,9 Tage · Drift 69,9 %)

| Klasse | Leg | Teilmenge | Events | zensiert | decided | WR % | Σ Move % | Ø netto %/Trade |
|---|---|---|---:|---:|---:|---:|---:|---:|
| v2_would_block | Trigger | **v1_agree** | 3.461 | 0 | **3.160** | 66,6 | **−274,9** | **−0,187** |
| v2_would_block | Trigger | v1_drifted | 1.387 | 0 | 1.346 | 59,3 | −1.000,4 | −0,843 |
| v2_would_block | **ROM1** | **v1_agree** | 3.461 | **2.010** | **1.342** | 81,2 | **+2,0** | **−0,099** |
| v2_would_block | ROM1 | v1_drifted | 1.387 | 915 | 469 | 82,5 | +64,0 | +0,037 |
| v2_would_open | Trigger | **v1_agree** | 124 | 0 | **88** | 86,4 | **+130,8** | **+1,386** |
| v2_would_open | Trigger | v1_drifted | 140 | 0 | 137 | 83,9 | +392,4 | +2,764 |
| v2_would_open | ROM1 | — | 264 | — | **0** | — | — | — |

### Fenster B (2026-07-25 → 2026-08-01, 7 Tage · Drift 85,8 %)

| Klasse | Leg | Teilmenge | Events | zensiert | decided | WR % | Σ Move % | Ø netto %/Trade |
|---|---|---|---:|---:|---:|---:|---:|---:|
| v2_would_block | Trigger | v1_agree | 1.813 | 0 | 1.537 | 67,7 | −158,2 | −0,203 |
| v2_would_block | **ROM1** | v1_agree | 1.813 | 941 | **763** | 82,0 | **−61,6** | **−0,181** |
| v2_would_open | Trigger | v1_agree | 101 | 0 | 67 | 88,1 | +76,7 | +1,044 |

### Die entscheidende Beobachtung

**Die beiden Messlatten widersprechen sich in der Größenordnung, das ROM1-Bein zusätzlich im Vorzeichen zwischen den Fenstern.**

- Trigger-Leg: v2 blockt Signale, deren Quell-Bots netto verloren haben (−0,187 %/Trade über 21 d, −0,203 % über 7 d) — konsistent negativ, aber **knapp** an der Round-Trip-Fee von 0,1 %.
- ROM1-Leg auf **denselben** Signalen: **+2,0 % Σ über 21 Tage** auf 1.342 entschiedene Trades (= +0,0015 %/Trade brutto) und **−61,6 % Σ über 7 Tage**. Das ist kein kleiner Edge, das ist Rauschen um Null.

Das ist P1.10 in Zahlen: **der Gate entscheidet auf der Statistik des Trigger-Bots, gehandelt wird ROM1-Geometrie.** Ein Bot kann in seiner Zelle verlieren und der daraus abgeleitete ROM1-Trade trotzdem nicht.

### Aufschlüsselung nach Bot × Richtung (Fenster A, Trigger-Leg, Top nach |Σ|)

**v2 würde zusätzlich blocken:**

| Bot | Dir | decided | Σ Move % |
|---|---|---:|---:|
| VolIndic | LONG | 658 | −570,0 |
| MIS1-72h | LONG | 193 | −226,3 |
| BR2H | LONG | 179 | −165,2 |
| **EPD3** | **SHORT** | 186 | **+137,8** |
| ATS2 | LONG | 134 | −126,3 |
| RUB2 | LONG | 25 | −106,3 |
| BR4H | LONG | 60 | −92,9 |
| EPD3 | LONG | 413 | −86,6 |
| **MIS1-168h** | **LONG** | 28 | **+63,7** |
| **RUB1** | **SHORT** | 22 | **+53,5** |

Der Flip ist also **kein sauberer Schnitt**: er nimmt VolIndic-LONG und MIS1-72h-LONG heraus (gut), kappt aber zugleich EPD3-SHORT, MIS1-168h-LONG und RUB1-SHORT (schlecht). Die vollständige Tabelle über alle betroffenen Beine steht in `staging_models/replay/whitelist_v2_realized_eval_2026-07-11.md`.

**v2 würde zusätzlich durchlassen** — die gesamte Klasse:

| Bot | Dir | decided (v1_agree) | Σ Move % |
|---|---|---:|---:|
| AIM2 | SHORT | 164 gesamt / 88 sauber | +473,8 gesamt / +130,8 sauber |
| SRA2 | SHORT | 61 | +49,3 |

Im 7-Tage-Fenster schrumpft AIM2-SHORT auf **7 entschiedene Trades**. Die „v2 schaltet Geld frei"-Seite ist eine **Einzelbein-Wette**, kein Portfolio-Effekt.

---

## 4. Was der Flip an Zensur mitkauft

Auf der ROM1-Seite sind **2.010 von 3.352 Legs (60 %) zensiert** — geschlossen durch `AUTO_CLOSE_ON_REGIME_CHANGE` (`CLOSED_REGIME_CHANGE`), also weder Win noch Loss (T-032-Konvention). Der Orchestrator schließt seine eigenen Trades so häufig regime-bedingt, dass **nur 40 % der geforwardeten Trades überhaupt ein bewertbares Outcome erreichen**. Jede Aussage über ROM1-Geld ruht auf diesen 40 %.

Nebenbefund, nicht Teil des Flip-Entscheids: `orchestrator_open_trades` zeigt über 60 Tage **6.500 `CLOSED_REGIME_CHANGE` gegen 4.421 lifecycle-geschlossene** Trades. Das deckt sich mit dem Step-6-Befund „Auto-Close kappt 49 % im Gewinn" und ist ein eigener Hebel, der **nichts** mit v1-vs-v2 zu tun hat.

---

## 5. Warum es keinen Out-of-Sample-Beleg gibt (das wichtigste Caveat)

**a) Der Trigger-Leg-Befund ist in-sample.** `27_bot_regime_analyzer` baut `bot_regime_performance` aus genau den geschlossenen Trigger-Trades der letzten `REFERENCE_WINDOW_DAYS = 30` Tage, und `_v2_whitelist_decision` entscheidet eine Zelle **allein** aus deren `avg_pnl_pct`/`pnl_stddev`. Die Fenster A und B liegen vollständig darin. Dass v2 dort Zellen blockt, deren Trigger-Trades negativ realisiert haben, ist **weitgehend eine Umformulierung von v2s Anpassungskriterium**, kein unabhängiger Beleg.

**b) Der Out-of-Sample-Lauf liefert nichts.** Fenster C (2026-05-15 → 2026-07-02, endet vor dem Fit-Fenster) enthält **0 Events der Klasse `v2_would_block`** — weil `orchestrator_open_trades.wl_reason` erst ab Anfang Juli befüllt wird (B8); die gesamte geforwardete Seite jener Ära trägt `NULL` und ist damit nicht zuordenbar. Die einzige dort vorhandene divergente Klasse (`v2_would_open`, 190 Events, **ausnahmslos EPD1-SHORT**) ist zu **100 % drift-kontaminiert**: EPD1 ist seit 2026-07-06 abgelöst, und die Zellen, die damals blockten, sind heute v1-offen. Ihr realisiertes Ergebnis (Σ −349,3 %, Ø −2,18 %/Trade) misst deshalb **nicht** v2 gegen v1.

**c) Die historische Whitelist bleibt nicht rekonstruierbar** (T-031-Befund, heute erneut geprüft und **bestätigt**): `bot_regime_whitelist` ist UPSERT-only ohne Historie, `bot_regime_performance` ebenso, und Bot 28 loggt pro Signal nur den **v1**-Pfad, nie den v2-Verdikt. Der v2-Verdikt pro Event muss deshalb aus dem heutigen Snapshot kommen. Die gemessene v1-Drift zeigt, was das kostet: **69,9 %** Übereinstimmung über 21 Tage, **85,8 %** über 7 Tage, **77,9 %** im Mai/Juni-Fenster.

**Daraus folgt konkret:** Solange das so bleibt, ist jede v2-Auswertung eine Näherung mit 14–30 % Klassifikationsfehler, und ein Rückblick nach einem Flip könnte **nicht** sauber rekonstruieren, was v1 getan hätte. Der Flip wäre nicht messbar rückabwickelbar — nur umschaltbar.

---

## 6. Wo die Vorbefunde stehen

| Vorbefund | Ergebnis der Messung |
|---|---|
| „Whitelist ist zu ~89 % default-open" (Step 6) | **bestätigt** auf Zellebene (88,7 %) — **aber irreführend als Aussage über den Traffic**: 81,8 % des zusätzlich geblockten Verkehrs kam über den Merit-Pfad (§2). |
| „SOFT-Gate-Counterfactual T-031: NO-EDGE für PnL bei −87 % Churn" | **derselbe Formbefund**, anderer Mechanismus: hier −55 % Durchsatz bei einem ROM1-Effekt von +2,0 % über 21 Tage. |
| „Der T-069-Flip ist starke Evidenz DAFÜR" (`docs/REGIME_CONDITIONED_GATING_EVAL.md` §5) | **hält der realisierten Messung nicht stand.** Die Analyse dort argumentiert auf regime-konditionierten Zell-Statistiken — also auf derselben Größe, aus der v2 gebaut ist. Gegen echte Forwards gemessen bleibt auf dem Geld-Bein kein Effekt übrig. |

---

## 7. Der Entscheid, der bei Michi liegt

Drei Optionen, alle mit den Zahlen oben belegbar:

**(A) Flippen.** Realistischer Erwartungswert nach dieser Messung: Durchsatz −55 %, ROM1-PnL-Effekt ununterscheidbar von Null, Verlust von EPD3-SHORT / MIS1-168h-LONG / RUB1-SHORT als Trigger, Gewinn einer Einzelbein-Wette auf AIM2-SHORT. Wer den Flip aus **Risikogründen** will (weniger Trades, weniger Exposure, weniger Slot-Verbrauch — vgl. das Slot-Budget aus T-042), hat dafür eine saubere Begründung; **aus PnL-Gründen belegen die Zahlen ihn nicht**.

**(B) Nicht flippen (Stop-B).** Die im Task vorgesehene gültige Antwort. v1 bleibt, v2 bleibt Shadow. Kostet nichts und verliert nichts, was die Messung belegen könnte.

**(C) Erst messbar machen, dann entscheiden.** Der Grund, warum (A) und (B) hier nicht sauber trennbar sind, ist **eine fehlende Zeile Logging**: Bot 28 schreibt beim Gate-Entscheid den v1-Pfad in `wl_reason` bzw. `reason`, liest die v2-Spalte aber nicht mit. Würde `get_whitelist_decision` den v2-Verdikt derselben Zelle **mitlesen und mitloggen**, wäre ab dem nächsten Restart jede Auswertung exakt statt genähert — Drift 0 %, kein Snapshot-Problem, und ein echtes A/B über die Zeit.

> **Nicht gebaut, bewusst.** Das ist eine Änderung im Geld-Pfad des Orchestrators und braucht einen Fleet-Restart, um zu wirken — beides außerhalb des Freigabe-Rahmens dieser Session. Der Einstiegspunkt ist `28_signal_orchestrator.get_whitelist_decision` (der bestehende `SELECT whitelisted, reason, computed_at` müsste `whitelisted_v2, reason_v2` mitziehen, und `log_suppressed`/`insert_orchestrator_trade` das Ergebnis in einer neuen, rein additiven Spalte ablegen). Wenn Michi (C) will, ist das ein eigener kleiner Task.

**Empfehlung, falls eine gewünscht ist:** (C) vor (A). Der Flip ist mit −55 % Durchsatz zu groß, um ihn auf eine in-sample-Messung mit 14–30 % Klassifikationsfehler und ohne jeden Out-of-Sample-Punkt zu stützen. Ist (C) zu aufwendig, ist (B) die konservative Antwort — v2 bleibt Shadow und kostet nichts.

---

## 8. Reproduktion

```
python tools/whitelist_v2_realized_eval.py --since 2026-07-11T00:00:00                                 # Fenster A
python tools/whitelist_v2_realized_eval.py --since 2026-07-25T00:00:00                                 # Fenster B
python tools/whitelist_v2_realized_eval.py --since 2026-05-15T00:00:00 --until 2026-07-02T00:00:00     # Fenster C
```

Alle drei Läufe sind read-only (`set_session(readonly=True)`, kein INSERT/UPDATE/DELETE im Tool), liefen unter `--force-on-busy` bei gemessenen 72,7 % / 90,4 % / 96,9 % System-CPU mit BELOW_NORMAL-Priorität und unter dem Job-Lock. Die Berichte liegen unter `staging_models/replay/whitelist_v2_realized_eval_*.md`.

**Zwei Lesehinweise zu den Zahlen:**
- Σ Move % ist der **ungehebelte**, target-gestaffelte realisierte Move (T-115-Definition) und die coverage-robuste Metrik. Die `Σ lev %`-Spalte in den Rohberichten ist **pro Trade bei −100 % geklammert** (`core.realized_pnl`, Liquidations-Boden) und deshalb nach oben verzerrt — nicht als „Geld" lesen.
- WR ist TP1-Touch, nicht Profitabilität; bei Ladder-TP-Bots ist ein Trade mit 66 % WR und negativem Move völlig normal.
