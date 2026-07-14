# Regime-konditioniertes Gating — Evidenz & Empfehlung (T-2026-CU-9050-125, Teil 3)

**Michis Frage:** Bei ROM (Bot 28) und AIM (Bot 15) werden Quellen/Modelle
pauschal gegated. Aber ein über den GESAMTZEITRAUM negativer Bot kann in der
PASSENDEN Marktphase (Regime) positiv laufen. Gibt es solche Quellen — und lohnt
ein **regime-abhängiges** Gating statt eines pauschalen Aus?

**Kurzantwort:** **Ja, das Phänomen ist real** — aber das Werkzeug dafür ist
bereits gebaut. Die richtige Antwort ist nicht „neuer Gate" und nicht „pauschal
aus", sondern **den bereits gebauten v2-EB-Shrinkage-Whitelist scharf schalten
(T-2026-CU-9050-069) auf frischen Daten**, plus Sichtbarkeit im AIM2-Report.
**Keine Live-Änderung in diesem PR** — nur Evidenz.

Reproduzierbar (read-only): `tools/regime_conditioned_gating_scan.py [--window 90|30] [--json]`.

---

## 1. Methode (read-only, keine Replays nötig)

Der stündliche Analyzer `27_bot_regime_analyzer.py` materialisiert bereits alles:

- **`bot_regime_performance`** — `avg_pnl_pct` / `win_rate` / `n_trades` je
  `(bot_name, regime, alt_context, direction, window_days)`. Die Zeile
  `(regime='ALL', alt_context='ALL')` ist die GLOBALE Erwartung eines Beins.
- **`bot_regime_whitelist`** — je `(bot, regime, alt_context, direction)` das v1-
  Gate (`whitelisted`) UND das Shadow-v2-Gate (`whitelisted_v2` / `reason_v2`).
  `reason_v2` trägt die **EB-Shrinkage-Untergrenze** (`lb`), den Punktschätzer
  (`est`), die Quelle (`src`, Zell- vs. Bot×Regime-Ebene) und das effektive n
  (`neff`).

„Global negativ, aber regime-positiv" = ein Bein mit `ALL/ALL avg_pnl_pct < 0`,
das in einer Regime-Zelle `avg_pnl_pct > 0` hat. „Robust" = die Zelle überlebt die
v2-Untergrenze (`lb > 0`), d. h. der positive Schnitt ist auch nach Shrinkage +
Mindest-n von Null unterscheidbar.

> **Datenstand-Caveat:** `bot_regime_performance`/`-whitelist` wurden zuletzt
> **2026-07-13 04:06** berechnet — kurz VOR dem ~14h-Ingestion-Ausfall dieses
> Tages, also ~1 Tag alt und mit ausgedünntem jüngstem Fenster. Vor jedem Flip
> müssen die Tabellen auf frischen Daten neu gerechnet werden (Bot-27-Uptime
> prüfen — s. `kythera-regime-orchestrator`).

---

## 2. Befund A — der Punktschätzer-KÖDER (warum „einfach anschalten" Geld verliert)

Auf den nackten Regime-Mittelwerten sieht vieles verlockend aus. Beispiel
**ATS1-LONG**: global −0,01 %, aber in `TRANSITION` **+1,47 %/Trade (n=258)**.

Splittet man aber nach `alt_context` und schrumpft (v2), kippt es:

```
ATS1 LONG  TRANSITION/ALT_NEUTRAL   est=+1.45%  lb=-0.263  -> v2_block
ATS1 LONG  TRANSITION/ALT_STRONG    est=+2.41%  lb=-2.462  -> v2_block
ATS1 LONG  TRANSITION/ALT_WEAK      est=+2.24%  lb=-1.491  -> v2_block
```

Der Punktschätzer ist positiv, die **Untergrenze bleibt negativ** — der positive
Schnitt ist bei dem n und der Varianz nicht von Rauschen zu trennen (Fett-Schwanz
einzelner Gewinner; mehrere solcher Zellen haben sub-50%-WR bei „positivem" Ø).
Ein naives „Regime-an, weil der Mittelwert positiv ist" würde genau diese
Rausch-Zellen handeln und verlieren. **v2 schrumpft sie korrekt weg.** Das
validiert das v2-Design und ist der Kern der Antwort: der Mittelwert allein ist
als Gate-Kriterium wertlos (MODEL_INTENT Regel 3).

---

## 3. Befund B — die DEFENDIERBAREN Zellen (18)

**18 Regime-Zellen liegen unter einem global-negativen Bein und überleben v2**
(`lb > 0`, window 90d). Das sind die Quellen, die pauschal-aus tatsächlich Geld
liegen lassen. Auszug (voll: Tool-Output):

| Bein (global) | Regime / alt | est | **lb** | neff |
|---|---|---|---|---|
| BR1H-LONG (−0,06%) | HIGH_VOLA / ALT_WEAK | +1,79% | **+1,39%** | **1505** |
| BR2H-LONG (−0,23%) | HIGH_VOLA / ALT_WEAK | +1,32% | +0,71% | 681 |
| EPD1-LONG (−0,32%) | TRANSITION / ALT_STRONG | +7,86% | **+4,21%** | 47 |
| RUB2-SHORT (−0,49%) | CHOP / ALT_NEUTRAL | +1,40% | +0,38% | 45 |
| RUB2-SHORT (−0,49%) | HIGH_VOLA / ALT_NEUTRAL | +3,20% | +0,24% | 37 |
| EPD2-SHORT (−0,04%) | CHOP/HIGH_VOLA (6 Zellen) | +1,9…3,8% | +1,5…2,7% | 25–35 |
| MIS1-8h-LONG (−0,03%) | HIGH_VOLA / ALT_WEAK | +3,19% | +3,03% | 27 |
| QM_1H/QM_4H-SHORT | HIGH_VOLA / CHOP | +0,6…1,0% | +0,2…0,5% | 25–27 |
| ATB1-SHORT (−0,77%) | TRANSITION / ALT_STRONG | +1,16% | +1,16% | 27 |
| SR-LONG (−0,19%) | HIGH_VOLA / ALT_STRONG | +0,11% | +0,11% | 27 |

Bemerkenswert: `BR1H-LONG / HIGH_VOLA·ALT_WEAK` mit **neff=1505** und lb +1,39 %
ist statistisch sehr solide; `EPD1-LONG / TRANSITION·ALT_STRONG` mit lb +4,21 %
ist ökonomisch groß. Von allen 132 v2-Pass-Zellen sind 85 SHORT / 47 LONG — es
gibt also robuste LONG-Regime-Zellen (v. a. HIGH_VOLA/TRANSITION), trotz des
short-lastigen Gesamtbilds.

---

## 4. Interpretation — das Werkzeug existiert schon

- **ROM (Bot 28)** liest `bot_regime_whitelist` je `(bot, regime, alt_context,
  direction)`. Der v2-EB-Shrinkage-Gate IST damit exakt ein regime-konditionierter
  Expectancy-Gate — er wird nur noch nicht LIVE gelesen (v1 ist live, v2 ist
  Shadow-Spalte). **Den T-069-Flip v1→v2 scharf schalten = das regime-abhängige
  Gating aktivieren.** Diese Analyse ist starke zusätzliche Evidenz DAFÜR.
- **AIM (Bot 15)** nutzt die Whitelist NICHT — es ist ein Meta-Modell mit EINEM
  globalen Threshold; die Regime-One-Hots sind Features, kollabieren aber auf eine
  Schwelle. `master_meta_model_aim2_report.json::per_source_test` ist zudem NUR
  über alle Regime gepoolt. AIMs Fix ist deshalb **Sichtbarkeit**: eine
  `per_source × regime`-Kreuztabelle im AIM2-Report (`groupby(["source", regime])`
  über dasselbe `te_meta`-Frame in `tools/aim2_train.py` — die Regime-One-Hots
  liegen schon in `X`). Ein regime-konditionierter Threshold wäre eine
  Modelländerung (eigener Task), keine Voraussetzung.

---

## 5. Empfehlung

1. **Kein neuer Gate, kein pauschales Aus.** Ein Blanket-off lässt die 18
   robusten Zellen liegen; ein naives Regime-an handelt die Köder-Zellen (Befund
   A). Der disziplinierte Mittelweg — Regime × alt × direction mit EB-Shrinkage —
   **ist bereits als v2-Whitelist gebaut.**
2. **T-2026-CU-9050-069-Flip v1→v2 vorantreiben**, aber **auf frisch gerechneten
   Tabellen** (Datenstand-Caveat §1). Das Eval-Tooling ist gebaut
   (`tools/whitelist_v2_flip_eval.py`, PR #116); dieser Scan liefert die
   inhaltliche Begründung.
3. **AIM2-Report um eine `per_source × regime`-Kreuztabelle erweitern** (billig,
   `tools/aim2_train.py`) — macht AIMs Regime-Verhalten überhaupt erst sichtbar.
4. **Kopplung an Teil 1 (Shadow-Mode):** Genau die geshadowten/unterdrückten
   Beine liefern ab jetzt die Regime-konditionierten Trade-Records, mit denen man
   ihre spätere Freischaltung belegt. Shadow-Posting und regime-konditioniertes
   Gating sind zwei Enden derselben Idee.

Restrisiken: Datenstand-Staleness (§1); `alt_context`-Split ist entscheidend (das
`ALL/ALL`-Roll-up überzeichnet — die Köder in Befund A); sub-50%-WR-Zellen sind
durch die lb-Bedingung bereits ausgeschlossen; ROM-Whitelist deckt die von ROM
re-forwardeten Bots, nicht das ganze AIM-Quelluniversum.
