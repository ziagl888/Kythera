# Position-Sizing-Spec — Fractional Kelly (destilliert aus CloddsBot `kelly.ts`)

**Task:** T-2026-CU-9050-057 · **Stand:** 2026-07-10 · **Status:** Design-Spec, **kein Live-Code**
**Quelle:** `alsk1992/CloddsBot` → `src/trading/kelly.ts` (Lizenz **MIT** — Copyright (c) 2026 alsk1992; Code-Referenz + Port erlaubt, Attribution im Port-Header Pflicht)
**Herkunft:** Repo-Audit 2026-07-10 (KB `mcp-41a50fe33552`)

---

## 0. Zweck & das Eine, was man zuerst wissen muss

Dieser Spec destilliert die Parametrik der `kelly.ts`-Positionssizing-Engine und prüft, ob und wo sie in Kythera andocken kann. **Der zentrale Befund vorab, weil er alles andere rahmt:**

> **Kythera sized heute keine Notional-Größe. Cornix tut das.** Kythera emittiert Telegram-Signale (Direction, **Leverage**, Margin:Cross, Entry/TP/SL) — *wie viel Kapital* pro Trade eingesetzt wird, entscheidet die Cornix-Money-Management-Konfiguration, nicht Kythera. `kelly.ts` dagegen berechnet exakt diese Notional-Größe (`positionSize = bankroll × kelly`). Ein 1:1-Port hätte in Kythera **keinen Hebel, an dem er zieht.**

Die verwertbare Substanz von `kelly.ts` ist deshalb nicht die `positionSize`-Zahl, sondern die **Adjustment-Kaskade**: die Logik, die eine rohe Kelly-Fraktion durch Drawdown, Streaks, Volatilität, Kategorie-Performance und Sample-Size moduliert. Diese Kaskade lässt sich auf einen Kythera-Hebel abbilden, der uns tatsächlich gehört (Leverage und/oder Orchestrator-Gating). Kapitel 4 zeigt die drei Andock-Optionen; Kapitel 6 gibt die Empfehlung.

---

## 1. Was `kelly.ts` tut — die Parametrik

### 1.1 Config-Parameter (`DEFAULT_CONFIG`)

| Parameter | Default | Bedeutung |
|---|---|---|
| `baseMultiplier` | `0.25` | **Quarter-Kelly** — die volle Kelly-Fraktion wird auf ¼ gestaucht (klassischer Fractional-Kelly-Schutz gegen Schätzfehler in `p`). |
| `maxKelly` | `0.25` | Harte Obergrenze der finalen Fraktion (nie mehr als 25 % Bankroll auf einen Trade). |
| `minKelly` | `0.01` | Harte Untergrenze (min. 1 %, sofern überhaupt gesized wird). |
| `lookbackTrades` | `20` | Rolling-Window für Win-Rate / Avg-Return / Vola. |
| `maxDrawdown` | `0.15` | Drawdown-Schwelle (15 %), ab der voll reduziert wird. |
| `drawdownReduction` | `0.5` | Faktor bei/über `maxDrawdown` — Fraktion halbiert. |
| `winStreakBoost` | `1.25` | Max-Boost bei Win-Streak (Anti-Martingale, „nach Gewinnen mehr"). |
| `winStreakThreshold` | `3` | Ab 3 Wins in Folge greift der Boost. |
| `volatilityScaling` | `true` | Vola-Ziel-Skalierung an/aus. |
| `targetVolatility` | `0.10` | Ziel-Vola (10 %); reale Vola über Ziel ⇒ kleiner, unter Ziel ⇒ größer. |

### 1.2 Die Roh-Kelly-Formel (`getBaseKelly`)

```
f = (b·p − q) / b        mit b = odds, p = Win-Prob, q = 1 − p
```

- `p` kommt entweder direkt aus einer bekannten Win-Rate **oder** wird aus einem `edge` geschätzt: `p = clamp(0.5 + edge/2, 0.05, 0.95)`.
- `odds = 1` (Binär-Default, Prediction-Market-Herkunft). **Für Kythera ist das der wichtigste Neu-Parameter** — siehe §3.2: Krypto-Perp-Trades haben ein asymmetrisches Reward/Risk (TP-Distanz vs. SL-Distanz), also `b = R = |TP−Entry| / |Entry−SL|`, nicht 1.

### 1.3 Die Adjustment-Kaskade (`calculate`, 9 Schritte)

Reihenfolge ist bedeutsam — die Faktoren multiplizieren sich:

1. **Base:** `kelly = fullKelly × baseMultiplier` (Quarter-Kelly).
2. **Confidence:** `× confidence` (Modell-/Signal-Konfidenz, 0..1).
3. **Drawdown:** ab 5 % Drawdown linear runter, bei ≥ `maxDrawdown` fix `× drawdownReduction`. Formel: `1 − (dd/maxDD)·(1−reduction)`.
4. **Win-Streak-Boost:** ab `winStreakThreshold` Wins `× min(1.25, 1 + (streak−thr+1)·0.05)`.
5. **Loss-Streak-Reduktion:** ab 2 Losses `× max(0.5, 1 − losses·0.1)`.
6. **Volatility-Scaling:** `× clamp(targetVol/realVol, 0.5, 1.5)`.
7. **Category-Adjustment:** wenn Kategorie ≥ 5 Trades hat und ±10 pp von der Gesamt-WR abweicht: Boost bis `1.2` / Reduktion bis `0.7`.
8. **Sample-Size:** < 10 Trades ⇒ `× (0.5 + n/10·0.5)` (weniger Vertrauen bei dünner Historie).
9. **Bounds:** `clamp(kelly, minKelly, maxKelly)`.

Danach: `positionSize = bankroll × kelly`, plus ein `confidence`-Score (0.4·Sample + 0.3·Performance + 0.3·(1−Drawdown)) und `warnings[]`.

### 1.4 State, den die Engine führt

`bankroll`, `peakBankroll` (→ Drawdown), `recentTrades[]` (Ring-Puffer der letzten `lookbackTrades`), `winStreak`/`lossStreak`, `categoryStats` (Map Kategorie → {wins, total, winRate}). Gefüttert über `recordTrade()` / `updateBankroll()` nach jedem Close.

---

## 2. Was Kythera heute hat (Ist-Aufnahme)

### 2.1 Die Sizing-relevanten Hebel

| Hebel | Wo | Was er tut |
|---|---|---|
| **Leverage-Cap (Markt)** | `core/market_utils.py:get_max_leverage` | Deckelt gewünschten Hebel auf `max_leverage.json` pro Symbol (Default 20x). |
| **Leverage-Cap (SL)** | `core/trade_utils.py:cap_leverage_to_sl` | R4-Fix: Hebel so cappen, dass Liquidation nie vor dem SL liegt (`lev ≤ safety/sl_dist`, safety=0.5 ⇒ Faktor 2). |
| **Trade-Geometrie** | `core/trade_utils.py:calculate_smart_targets` u.a. | Entry/Entry2/SL/TP aus S/R-, Fib-, HVN-, FVG-Clustern + ATR-Caps (SL ≤ 15 %, E2 ≤ 10 %). |
| **Signal-Gating** | `28_signal_orchestrator.py` | Regime-Whitelist + Dedupe; entscheidet, **ob** ein Signal überhaupt gepostet wird. |
| **Notional / Margin pro Trade** | **Cornix** (extern) | **Nicht in Kythera.** Kythera hat keinen `bankroll`, keine Order-Size-Berechnung. |

### 2.2 Das State-Substrat existiert bereits

Der wichtigste Anschlusspunkt: **Kythera berechnet die Performance-Historie, die eine Kelly-Kaskade braucht, schon heute** — in `27_bot_regime_analyzer.py`:

- Pro **Bot × BTC-Regime × Alt-Context × Direction** über rollende Fenster `[7, 30, 90]` Tage:
  - `win_rate` (%), `avg_pnl_pct`, `median_pnl_pct`, `sharpe = avg_pnl/stddev`, `n_trades`
  - Persistiert in `bot_regime_performance` (UPSERT), Schwelle `MIN_TRADES_FOR_DECISION = 30`.
- Trade-Outcomes werden **PnL-basiert** klassifiziert (`win`/`loss`/`neutral`), Neutrale (|pnl| ≤ 0.1 % Housekeeping, > 100 % Datenbug) fliegen raus — sauberer als `targets_hit`.

Das ist eine **fast deckungsgleiche Abbildung** auf `kelly.ts`-State:

| `kelly.ts` | Kythera-Äquivalent | Status |
|---|---|---|
| `recentWinRate` (lookback 20 Trades) | `bot_regime_performance.win_rate` (Fenster 7/30/90 Tage) | ✅ vorhanden, andere Fenster-Definition (Zeit statt Trade-Count) |
| `recentVolatility` | `stddev` der PnL (in `sharpe` bereits berechnet) | ✅ vorhanden (nicht separat persistiert, trivial nachzuziehen) |
| `recentAvgReturn` | `avg_pnl_pct` | ✅ vorhanden |
| `categoryWinRates` (Kategorie = z.B. Coin-Klasse) | Bot × Regime × Direction ist die natürliche „Kategorie" | ✅ vorhanden, feinere Granularität |
| `bankroll` / `peakBankroll` / `currentDrawdown` | — | ❌ **fehlt** (Kythera trackt kein Kapital) |
| `winStreak` / `lossStreak` | — | ❌ **fehlt** (ableitbar aus `ai_signals`-Close-Historie, nicht materialisiert) |

**Konsequenz:** Die win-rate-/vola-/kategorie-getriebenen Adjustments (Schritte 2,6,7,8) sind in Kythera **datenseitig sofort baubar**. Die kapital-getriebenen Adjustments (Schritte 3,4,5 — Drawdown, Streaks) brauchen entweder ein Bankroll-/Streak-Tracking, das Kythera heute nicht führt, **oder** eine Umdeutung von „Drawdown/Streak" auf Bot-Ebene (rollende PnL-Kurve pro Bot statt Konto-Equity).

---

## 3. Der Port nach Python — Spec (nicht Implementierung)

### 3.1 Form-follows-Function-Vorgaben (Kythera-Regeln)

Ein späterer Port **muss**:

- **Reines Modul in `core/`** sein (z.B. `core/kelly_sizing.py`), damit — falls Kelly je in Trainer/Replay/Backtest einfließt — Serving == Replay gilt (Harte Regel 7 / Falle 2). Solange Kelly nur Live-Leverage skaliert, ist das noch keine Feature-Builder-Kopplung; wird es aber in Backtests zur Bewertung genutzt, gilt die Ein-Quelle-Regel.
- **Pure function + Dataclass-Config** sein, kein Objekt mit verstecktem Mutable-State im Bot. Der `kelly.ts`-Closure-State (`recentTrades`, `winStreak`, …) wird in Kythera **nicht im Prozess gehalten**, sondern pro Call aus `bot_regime_performance` / `ai_signals` gelesen (DB ist die Wahrheitsquelle, nicht ein Bot-lokaler Ring-Puffer).
- **MIT-Attribution** im Header tragen (`# Portiert aus alsk1992/CloddsBot src/trading/kelly.ts (MIT). …`).
- **Default-off** ausgeliefert werden (Batch-E-/`z-fable-judgment`-Disziplin: erst billig falsifizieren, dann Live-Code).

### 3.2 Nötige Anpassungen gegenüber `kelly.ts`

1. **`odds` ist nicht 1.** Krypto-Perp: `b = R = geplante TP-Distanz / SL-Distanz`. Kythera kennt beides zum Signal-Zeitpunkt (`calculate_smart_targets` liefert Entry/SL/TP). Ohne diese Korrektur unterschätzt die Kelly-Formel R>1-Trades systematisch.
2. **Multi-TP-Realität.** Kythera-Signale haben bis zu 10 Targets mit Teil-Exits (Cornix skaliert raus). Das effektive `R` ist ein gewichteter Blend über die TP-Leiter, nicht `TP1`. Für einen ersten Wurf: `R` konservativ auf TP1 ansetzen (unterschätzt eher → sicherer).
3. **„Category" = Bot × Regime × Direction**, nicht Coin-Kategorie. Die Granularität existiert schon in `bot_regime_performance`.
4. **Drawdown/Streak umdeuten** (siehe §2.2): auf die rollende Bot-PnL-Kurve statt Konto-Equity — oder in Phase 1 ganz weglassen und nur die datenseitig vorhandenen Adjustments (WR/Vola/Category/Sample/Confidence) portieren.

### 3.3 Signatur-Skizze (illustrativ, nicht final)

```python
@dataclass(frozen=True)
class KellyConfig:
    base_multiplier: float = 0.25   # Quarter-Kelly
    max_kelly: float = 0.25
    min_kelly: float = 0.01
    target_volatility: float = 0.10
    vol_scaling: bool = True
    # Drawdown/Streak-Parameter nur, wenn Phase-2-State vorhanden

def kelly_fraction(
    win_rate: float,          # aus bot_regime_performance
    reward_risk: float,       # R = TP-Dist / SL-Dist  (NEU ggü. kelly.ts)
    confidence: float,        # Modell-/Signal-Konfidenz
    recent_vol: float,        # stddev der PnL
    n_trades: int,            # Sample-Size-Gate
    cfg: KellyConfig = KellyConfig(),
) -> float:
    """Reine Kelly-Fraktion in [min_kelly, max_kelly]. Kein State, kein I/O.
    Portiert aus alsk1992/CloddsBot src/trading/kelly.ts (MIT)."""
    ...
```

Diese Fraktion ist **noch keine Order-Size** — was mit ihr geschieht, klärt Kapitel 4.

---

## 4. Andock-Optionen — worauf die Kelly-Fraktion wirkt

Da Kythera keine Notional-Größe stellt, muss die Fraktion auf einen Hebel abgebildet werden, der Kythera gehört. Drei Optionen:

### Option A — Kelly → Leverage-Skalierung
Die Fraktion moduliert den gewünschten Hebel **innerhalb** des bestehenden Envelopes:
`lev = round(base_lev × (kelly / max_kelly))`, danach unverändert durch `get_max_leverage` **und** `cap_leverage_to_sl`.
- **Pro:** nutzt einen Hebel, der Kythera schon besitzt; Risk-Envelope (SL-Cap) bleibt hart; keine Cornix-Änderung.
- **Contra:** Leverage ≠ Positionsgröße bei Cross-Margin — höherer Hebel erhöht nur die Liquidationsnähe, nicht zwangsläufig das eingesetzte Kapital, wenn Cornix eine feste Margin/Order-Size fährt. Der Risiko-Effekt hängt an der Cornix-Config und ist **nicht** sauber „Kelly-Fraktion = Kapitalanteil". **Semantik muss vor Bau geklärt werden** (siehe §6, offene Frage 1).

### Option B — Kelly → Orchestrator-Gating (Size-as-Inclusion)
Statt die Größe zu variieren, variiert man die **Postingdichte**: nur posten, wenn `kelly ≥ threshold`; niedrige Fraktion = Signal fällt raus. Das erweitert die bestehende Regime-Whitelist in `28_signal_orchestrator` um eine kontinuierliche Kelly-Schwelle.
- **Pro:** vollständig innerhalb Kytheras Kontrolle; keine Notional-/Leverage-Semantik-Frage; direkt gegen `bot_regime_performance` messbar; passt zur „ob überhaupt posten"-Rolle des Orchestrators.
- **Contra:** ist streng genommen kein *Sizing*, sondern *Selektion*. Der Kelly-Kern (kontinuierliche Größe) geht verloren; man nutzt nur die Kaskade als Qualitäts-Score.

### Option C — Kelly → Cornix per-Signal-Risk
Falls Cornix ein per-Message-Risk-/Size-Feld parst (z.B. „Risk: X%"), könnte die Fraktion direkt in den Signal-Block. **Ungeprüft** — Cornix-Message-Format-Capability muss verifiziert werden, bevor das eine Option ist. Harte Regel 4 (genau eine Cornix-parsebare Message) bleibt bindend.
- **Pro:** einziger Weg, echtes Notional-Sizing zu erreichen, ohne Kythera ein Kapital-Modell zu geben.
- **Contra:** hängt an unbestätigter Cornix-Funktionalität; berührt den Money-Path direkt (Doppel-Trade-Risikoklasse).

---

## 5. Abgleich CloddsBot ↔ Kythera (Zusammenfassung)

| Dimension | `kelly.ts` (CloddsBot) | Kythera heute |
|---|---|---|
| Sized was? | Notional (`bankroll × kelly`) | nichts (Cornix); nur Leverage + Geometrie |
| Roh-Kelly | `(b·p−q)/b`, `b=1` (binär) | müsste `b=R` (asymmetrisch) nutzen |
| Win-Rate-State | Ring-Puffer 20 Trades, in-process | `bot_regime_performance`, DB, Fenster 7/30/90d ✅ |
| Vola-State | stddev der 20 letzten Returns | `sharpe`/stddev vorhanden ✅ |
| Kategorie | frei (Coin-Klasse), Map in-process | Bot×Regime×Direction, DB ✅ (feiner) |
| Drawdown | Konto-Equity vs. Peak | kein Equity-Tracking ❌ |
| Streaks | in-process Counter | nicht materialisiert (ableitbar) ❌ |
| Fractional-Schutz | Quarter-Kelly + max/min-Clamp | analog übernehmbar ✅ |
| Ausführung | direkt an Exchange-Adapter | via Telegram→Cornix (indirekt) |

**Kernaussage:** Die *Statistik-Hälfte* der Engine (WR/Vola/Kategorie/Sample → Fraktion) ist in Kythera datenseitig **fast geschenkt**. Die *Kapital-Hälfte* (Drawdown/Streak/Notional) hat in Kythera **kein Fundament** — dort sitzt Cornix, und ohne Bankroll-Modell fehlt der Bezugsrahmen.

---

## 6. Empfehlung

**Kurz: Kelly nicht als Notional-Sizer bauen. Die Adjustment-Kaskade als bot-seitigen Qualitäts-/Konfidenz-Score adaptieren — und zwar erst hinter einem Replay-Beweis, nicht spekulativ.** Begründung nach `z-fable-judgment`:

- **Outcome:** Wollen wir *variable Positionsgröße* oder *bessere Selektion*? Ein echter Notional-Sizer setzt voraus, dass wir Cornix die Größe abnehmen (Option C, ungeprüft) oder Kythera ein Kapital-Modell geben (großer, irreversibler Schritt Richtung eigener Execution). Beides ist **out of scope** eines 2h-Low-Prio-Tasks und **Operator-Entscheidung** (Eskalation §6 Handoff: berührt Geld-Path/Architektur).
- **Billigste Falsifikation zuerst:** Bevor Kelly *irgendeinen* Live-Hebel bewegt, den Backtest fahren: Verändert eine Kelly-Fraktion (aus `bot_regime_performance`) als **Post-hoc-Gewichtung** auf die Walk-Forward-Replay-PnL das Ergebnis überhaupt zum Besseren? Wenn nein → No-op, Done. Wenn ja → welche Adjustments tragen den Effekt (WR? Vola? Category?).
- **Empfohlene Ausbaustufe (falls der Replay-Beweis positiv ist):** **Option B** (Kelly-Score als kontinuierliche Orchestrator-Schwelle) — vollständig in Kytheras Kontrolle, keine Cornix-/Notional-Semantik-Falle, direkt gegen vorhandene Daten messbar, default-off via Gate.
- **Option A (Leverage-Skalierung) nur**, wenn §6-Frage 1 (Leverage↔Kapital-Semantik unter der realen Cornix-Config) sauber geklärt ist — sonst skaliert man Liquidationsnähe statt Risiko.

### Offene Fragen an Michi (Eskalation)

1. **Cornix-Money-Management:** Fixe Margin/Order-Size pro Trade, oder %-Risk? Davon hängt ab, ob Option A überhaupt einen Sizing-Effekt hat und ob Option C existiert.
2. **Soll Kythera je eigenes Notional-Sizing bekommen** (= Schritt weg von „Cornix macht Money-Management")? Strategische, irreversible Richtungsfrage — nicht in diesem Task entscheidbar.
3. **Drawdown/Streak-Definition:** Konto-Equity (gibt es nicht) vs. rollende Bot-PnL-Kurve (baubar) — nur relevant, wenn Kelly über die Statistik-Adjustments hinaus soll.

### Nächster konkreter Schritt (kein Live-Eingriff)

Ein Batch-E-Studien-Task (Vorlage T-2026-CU-9050-020, HMM-Studie): reine Kelly-Fraktion aus `bot_regime_performance` rechnen, als Gewichtung auf die vorhandene Walk-Forward-Replay-PnL legen, Effekt messen. Entscheidet in ~1 Tag über Bau/No-op — **bevor** eine Zeile Live-Sizing-Code entsteht.

---

*Attribution: Adaptiert aus `alsk1992/CloddsBot` `src/trading/kelly.ts`, MIT License (Copyright (c) 2026 alsk1992). Dieser Spec ist Design-Doku; er portiert keinen Code in den Live-Pfad.*
