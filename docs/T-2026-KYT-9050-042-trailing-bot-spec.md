# T-2026-KYT-9050-042 Phase C — Trailing-Close-Bot (eigener Telegram-Channel)

**Stand:** 2026-07-26 · **Status:** gebaut, reviewt, **NICHT deployt** (Fleet-Start ist Michi-Entscheid)
**Vorlauf:** T-041 Phase A (Verdampfung belegt), PR #198 (Slot-Budget, Bein-Auswahl)

## Intent

Ein neuer Fleet-Prozess (`40_trailing_close_bot.py`) spiegelt die Signale der in PR #198
ausgewählten Beine in einen **eigenen Telegram-Channel** und schließt sie dort per
Trailing-Close, statt sie bis SL/TP laufen zu lassen. Michi hängt Cornix selbst an diesen
Channel — damit läuft der Trailing-Arm live gegen den Hold-Arm der bestehenden Fleet,
ohne dass ein einziger bestehender Bot sein Verhalten ändert.

Der Bot ist ein **reiner Beobachter der Fleet**: er liest `ai_signals`, schreibt dort nie
hinein, und schließt keinen bestehenden Trade. Sein einziges Schreibrecht sind
`telegram_outbox` (sein eigener Channel) und seine eigene Tabelle `trailing_positions`.

## Betriebspunkt (Operator-Entscheid Michi, 2026-07-26)

| Parameter | Wert | Herkunft |
|---|---|---|
| Aktivierungsschwelle `act` | **2,0 %** unlevered Peak | `trailing_slot_budget_live.json`, p95-sichere Füllung |
| Rückgabe `x` | **10 %** vom Peak | T-041/T-046 Sweep |
| Beine | **33** (p95-Auswahl bei act=2) | `fills_by_act["2.0"]["p95"]["accepted"]` |
| Erwartung | 49 204 % netto, Ø 285 / p95 498 Slots | ebd. |
| Slot-Cap | 500 (Cornix, pro Channel) | Operator |

## Akzeptanzkriterien (binär testbar)

- [x] **AK1** — Nur Beine aus dem Roster werden gespiegelt; ein Signal eines nicht
  ausgewählten Beins erzeugt keinen Eintrag. *Test:* `test_roster_admits_only_selected_legs`
- [x] **AK2** — Ein Bein, dessen `shadow_gate.leg_status` nicht LIVE ist, wird auch dann
  nicht gespiegelt, wenn es im Roster steht (Register schlägt Roster).
  *Test:* `test_non_live_leg_is_never_mirrored`
- [x] **AK3** — Pro Symbol hält der Channel **höchstens eine** Position. Ein zweites Signal
  auf demselben Symbol wird mit Grund abgewiesen, nicht still verworfen.
  *Test:* `test_second_signal_on_same_symbol_is_rejected`
  *Warum:* Cornix' `Close <SYMBOL>` wirkt **symbol-weit** (`core/config.py:123`) — bei zwei
  Positionen auf einem Symbol würde der Trailing-Exit der einen die andere mit flachmachen.
  Das ist derselbe Konflikt, den `28_signal_orchestrator.py:1562` durch Zurückstellen löst;
  hier ist Zurückstellen falsch, weil der rechtzeitige Exit der ganze Zweck des Bots ist.
- [x] **AK4** — Der Channel überschreitet den Slot-Cap nie; die Ablehnung entscheidet der
  Bot nach Bein-Dichte, nicht Cornix nach Zufall. *Test:* `test_slot_cap_rejects_by_density`
  *Warum:* die gewählte p95-Auswahl hat eine Belegungs-Spitze von **2001** = 4× Deckel
  (`trailing_slot_budget_live.md:82`). Ohne eigene Zulassungskontrolle entscheidet in der
  Spitze Cornix, welche ~1500 Trades abgelehnt werden.
- [x] **AK5** — Der Trail feuert erst, wenn der Peak `act` überschritten hat; ein Trade, der
  nie 2 % im Plus war, wird nie getrailt. *Test:* `test_activation_floor_gates_the_trail`
- [x] **AK6** — Die Live-Trailing-Entscheidung ist identisch zur Studien-Logik: dieselbe
  Mark-Folge durch `TrailingState` und durch `core.wave_exit_sim.trailing_tp_trigger`
  ergibt denselben Auslöse-Index. *Test:* `test_live_state_matches_wave_exit_sim` (Regel 7)
- [x] **AK7** — Der laufende Peak überlebt einen Prozess-Neustart (sonst re-armt der Trail
  auf dem gefallenen Mark und ein längst verdampfter Gewinn wird nie geschlossen).
  *Test:* `test_peak_survives_restart`
- [x] **AK8** — Schließt die Fleet den Quell-Trade (SL/TP/Timeout), schließt der Bot seine
  Spiegel-Position ebenfalls. *Test:* `test_source_close_mirrors_into_a_close`
- [x] **AK9** — Genau **eine** Cornix-parsebare Nachricht pro Entry (Harte Regel 4); die
  Info-Nachricht und die Exit-Nachricht sind nicht als Entry parsebar.
  *Test:* `test_exactly_one_parsable_message_per_entry`
- [x] **AK10** — Der Cornix-Block ist **byte-identisch** zu dem, den `core.signal_post`
  für dieselbe Geometrie erzeugt (eine Quelle, Regel 7 — die entry2-Änderung aus PR #197
  darf sich nie nur auf einer Seite fortpflanzen). *Test:* `test_cornix_block_is_shared`
- [x] **AK11** — `TRAILING_BOT_LIVE_POSTING` ist **default 0**: ohne expliziten Operator-Flip
  läuft der Bot vollständig, trackt und loggt, schreibt aber **keine** Outbox-Zeile.
  Ein ungesetzter Channel (`0`) erzwingt Shadow ebenfalls.
  *Test:* `test_default_is_shadow_only`
- [x] **AK12** — Der Bot schreibt nie in `ai_signals` und schließt nie einen Fremd-Trade.
  *Test:* `test_bot_never_writes_ai_signals` (Quelltext-Negativbeweis)

## Out of Scope

- **Kein Deploy, kein Fleet-Restart.** Der Watchdog liest `core/fleet.py` beim Import;
  der neue Eintrag wird erst nach einem Michi-gegateten Restart supervised.
- **Keine Cornix-Konfiguration.** Ob und wie Cornix an den Channel gehängt wird, macht Michi.
- **Kein DCA/Multi-Entry.** Der Spiegel postet single-entry (Arm B, PR #197).
- **Kein eigenes Modell.** Der Bot trifft keine Einstiegsentscheidung — er spiegelt.
- **Keine 5m/10s-Verschärfung der Studie.** Der Bot sieht echte Preise; die
  Auflösungs-Grenze aus PR #198 betrifft nur die Simulation.

## Warum bauen statt wiederverwenden (Phase 0b)

Kein bestehender Bot spiegelt Fremd-Signale in einen zweiten Channel. Wiederverwendet
werden dagegen: `core.wave_exit_sim.trailing_tp_trigger` (Trailing-Semantik, per Pin
gebunden), `core.signal_post` (Cornix-Block, per Extraktion geteilt),
`core.live_price.get_live_prices_batch` (ein Binance-Call pro Poll),
`core.bot_naming.pretty_name` + `core.shadow_gate` (Bein-Identität), die Poll-Schleifen-
und Reconnect-Form aus `8_ai_trade_monitor.py`.

## Bekannte Grenzen

- **Symbol-Eindeutigkeit kostet Signale.** AK3 wirft das zweite Signal je Symbol weg. Die
  Alternative wäre ein Channel pro Bein (33 Channels) — Operator-Entscheid, nicht Code.
- **Slippage ist auch live nicht modelliert**, aber jetzt real: die Exits sind klein und
  zahlreich (bei act=2 % Median-Haltedauer 4,6 h).
- **Der Roster ist ein Standbild** des Registers vom 2026-07-26. Ändert `shadow_gate` ein
  Bein, greift AK2 (Register schlägt Roster) — der Roster selbst wird nicht automatisch nachgezogen.
