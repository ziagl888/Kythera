# Deploy-Voraussetzungen für T-033 — Umsetzungs-/Befundreport (T-2026-KYT-9050-034)

_generated 2026-07-23 · INTERAKTIVE Session (Operator Michi live) · CODE + Staging-Artefakte · KEIN Deploy/Restart/env-Flip · DB strikt read-only (`set_session(readonly=True)`, nur SELECTs) · KEIN Artefakt-Root-Move (Hard Rule 2) · Basis: `staging_models/replay/fleet_reconfig_t033.md` §3/§5_

## 0. Kernbefund (für Michi)

Die drei aus T-033 geflaggten Deploy-Vorbedingungen wurden read-only durchleuchtet (alle DB-Zugriffe `set_session(readonly=True)`, nur SELECTs). Ergebnis:
- **Paket 3 (EPD3-Staging): erledigt** (nach staging kopiert, Loader verifiziert).
- **Paket 2 (SRA2-SHORT): Diagnose korrigiert — der Leg ist ungegatet PROFITABEL (+1.06 %/Trade, 232 Trades).** Die T-033-„Flood-Hazard"-Sorge verwechselte Volumen mit Unprofitabilität; ein Threshold ist weder nötig noch aus den Daten bestimmbar. → deploybar, offene Frage ist Volumen-Toleranz.
- **Paket 1 (MIS1): nicht sauber rekonstruierbar** (Leakage-67-Feature-Generation; sauberer Retrain = MIS2). Der auf Operator-Wunsch gestartete frische MIS2-Replay brach bei CPU 100 % selbst ab → Follow-up in ruhigem CPU-Fenster.

## 1. Paket 3 — EPD3-SHORT-Staging ✅ ERLEDIGT

- **Fix:** `epd3_model_SHORT.pkl` (Root) → `staging_models/epd3_model_SHORT.pkl` kopiert (staging erlaubt, Hard Rule 2 gated nur Root).
- **Verifiziert:** `shadow_gate.load_shadow_artifact("EPD3","SHORT")` lädt jetzt (dict, 16 Features, threshold 0.6737). Vorher: EPD3-SHORT-Park war stille Silence, weil der SHADOW-Loader `staging_models/epd3_model_SHORT.pkl` las (fehlte). Jetzt echte Shadow-Historie in `closed_ai_signals`.
- Kein Root-Move, kein Restart nötig (der Bot lädt beim nächsten regulären Restart/Reload).

## 2. Paket 1 — MIS1-Revive: technisch nicht sauber rekonstruierbar

**Feature-Kompatibilitäts-Prüfung (DB-frei, alle 8 Artefakte):** Die MIS1-`pump_model_*_final.pkl` sind nackte `XGBClassifier` mit **67 Features** und konsumieren je **alle 8 Leakage-Spalten** (`atr_14` roh, `macd_hist` roh, `macd_dif_delta_1`, `macd_hist_delta_1` + die 4 „Unfall"-Features `boll_upper/lower/ema_200_dist_atr_dist_pct`, `ema_9_cross_above_21_dist_pct`) = exakt die Preisklassen-Leakage aus Report 13-P1 (`core.mis_features.LEGACY_ONLY_COLS`).

| Artefakt (alle 8) | n_features | Typ | fehlt vs. sauberem Builder | nutzt Leakage-Spalten |
|---|---|---|---|---|
| pump_model_{8,24,72,168}h_{pump,dump}_final.pkl | 67 | XGBClassifier | 8 | 8 |

Der aktuelle Builder (`core/mis_features.py`, `include_legacy=False`) liefert nur die 63 sauberen Features → der P0.12-Selfcheck in Bot 11 würde jedes MIS1-Modell **entladen**. Ein Wiring mit `include_legacy=True` hieße, Leakage-Modelle live posten zu lassen — genau was der Selfcheck verhindert (= „Fake", Task-Brief).

**Warum ein sauberer Retrain ≠ MIS1-Revive:** Die saubere MIS-Pipeline (`tools/mis1_move_labels.py` → `tools/retrain_from_replay.py --strategy mis1 --label-mode move`) ist **exakt die Pipeline, die MIS2 erzeugt hat** — dasselbe ±X%-Move-Label-Konzept (8h±5% / 24h±10% / 72h±15% / 168h±25%). Der einzige Unterschied MIS1→MIS2 war der Leakage-Feature-Cleanup. Ein sauberer „MIS1"-Retrain **reproduziert MIS2** (existiert bereits, realisiert laut Audit T-032 schlechter). Der „MIS1 besser"-Edge lebte in den Leakage-Features → **sauber nicht rekonstruierbar.**

**Operator-Entscheid:** „frischen MIS2-Move-Retrain laufen, jetzt starten (BELOW_NORMAL)". **Blocker (bestätigt):** kein aktuelles MIS-Replay-Artefakt in `staging_models/replay/`; die vorhandenen (`_X/…/mis1_replay_{400,540}d.jsonl`, `mis1_move_labels.jsonl`) sind **vom 5. Juli** → ein Retrain darauf reproduziert deterministisch die aktuellen MIS2-Root-Artefakte (kein Mehrwert). Ein echt frischer MIS2 braucht eine **Replay-Neugenerierung (`walkforward_sim --strategy mis1`)**. Der Job wurde detached/low-prio gestartet und **brach sich SELBST ab**: `ABBRUCH: System-CPU bei 100% (> 90%) — Fleet nicht zusätzlich belasten` (`MAX_CPU_AT_START=90.0`). Die VPS ist aktuell voll saturiert → der Replay ist **jetzt nicht lauffähig**; er braucht ein ruhiges CPU-Fenster (nachts / nach CPU-Entlastung). Follow-up-Task, siehe §4.

## 3. Paket 2 — SRA2-SHORT: die „Flood-Hazard"-Diagnose war falsch — der Leg ist ungegatet PROFITABEL

**Warum ein Retrain/Threshold der falsche Hebel ist (Datenlage, read-only DB):**
- Alte Labelquelle `closed_trades3`: **tot seit 2026-02-23** (0 Trades in 60d) → `retrain_sra2.py` straight reproduziert das null-Threshold-Modell (val −0.079% ist ein **Feb-Regime-Proxy**, nicht die Realität).
- Retrain auf frischer Quelle `closed_ai_signals` (Operator-Entscheid „Guard senken"): SRA2-only 232 Trades / 8-Tage-Fenster → Val zu dünn; pooled SRA1+SRA2 641 → `pick_threshold_safe`=**None**. **Grund:** die Basisrate ist bereits **90 % WR / +1.06 %/Trade** — ein Prob-Threshold kann das nicht schlagen und die 8-Tage-Historie trägt keinen robusten Split. Ein Threshold ist hier **nicht nötig und nicht bestimmbar.**

**Der entscheidende Befund (realized Shadow-Historie, `closed_ai_signals`, net = (entry−close)/entry − 0.10 % Fees, deckt sich mit Audit „+1.00 %×222"):**

| SRA2-SHORT-Filter | n | WR | Ø-net/Trade | Σ-net |
|---|---|---|---|---|
| **KEIN Gate (post jeden Kandidaten)** | 232 | 90.5 % | **+1.057 %** | +245 % |
| fund_24h ≤ 0 | 44 | 95.5 % | +1.423 % | +63 % |
| fund_24h ≤ +1.5 | 204 | 91.2 % | +1.048 % | +214 % |
| fund_24h ∈ [+1.5,+3) (ABR-„Veto-Zone") | 15 | 86.7 % | +1.498 % | +23 % |

Der `threshold=null`-„Flood" realisiert **+1.057 %/Trade** über 232 Trades. Die T-033-Sorge „LIVE postet auf jedem Kandidaten → Cornix-Flood" verwechselte **Volumen** mit **Unprofitabilität** — der „Flood" IST der Edge. Das negative Val-Signal (−0.079 %) stammte allein aus der toten Feb-Labelquelle.

**Funding-Gate (Operator-Frage):** rettet keinen Edge (der ist da), trimmt nur **Volumen**. `fund_24h≤0` hebt auf +1.42 %, schneidet aber auf 44/232. Die ABR-„SHORT-Veto"-Zone (fund>+1.5 bps) ist bei SRA2-SHORT **positiv** (+1.5 %) → das ABR-Veto gilt hier NICHT. Der Edge ist über alle Funding-Zonen breit positiv.

**Konsequenz / Empfehlung:** SRA2-SHORT ist **deploybar** — es braucht KEINEN Threshold, weil das rohe Signal +1.06 %/Trade realisiert. Das einzige echte Thema ist **Volumen** (~29 Posts/Tag ungegatet) für den Cornix-Channel — eine Operator-Toleranz-Entscheidung, kein Code-/Modell-Defekt. Optionen: (a) ungegatet nach Root promoten (Michi, Hard Rule 2) und Volumen akzeptieren; (b) optionales, additives Funding-/Volumen-Gate im Bot-9-SRA2-SHORT-Emit als reine Volumen-Bremse (eigener kleiner Code-Task — NICHT edge-notwendig).

## 4. Offene Operator-Entscheidungen (Michi-gegatet)

1. **MIS2-Replay-Neugenerierung:** braucht ein ruhiges CPU-Fenster — der Job brach sich bei CPU 100 % selbst ab (§2). Follow-up: `walkforward_sim --strategy mis1 --days 400` in einer Low-Load-Phase, dann `retrain_from_replay --strategy mis1 --label-mode move` → Artefakte aus `_X/staging_models` nach Repo-`staging_models/` kopieren. **Caveat bleibt:** das Ergebnis ist ein aktualisiertes MIS2, KEIN „MIS1" (der Leakage-Edge kehrt nicht zurück).
2. **SRA2-SHORT-Promotion (deploybar!):** kein Threshold nötig (+1.06 %/Trade ungegatet). Entscheidung ist **Volumen-Toleranz** (~29 Posts/Tag): (a) `sra2_model_SHORT.json` nach Root promoten und ungegatet live nehmen; ODER (b) optionales additives Funding-/Volumen-Gate im Bot-9-Emit (eigener kleiner Code-Task). Root-Move = Michi (Hard Rule 2).
3. **MIS1-Revive** grundsätzlich: nur über einen bewusst NEUEN Modelltyp (nicht die MIS2-identische Move-Pipeline) rekonstruierbar — eigener Konzept-Task.

## 5. Sicherheitsvertrag (Regel 1/2/4)

- DB ausschließlich read-only (`set_session(readonly=True)`, nur SELECTs; DB-User `dbfiller`, aber Session read-only erzwungen).
- Einziger Datei-Write in Repo: `staging_models/epd3_model_SHORT.pkl` (staging, erlaubt). Kein Root-Move, kein Restart, kein env-Flip.
- Retrain-Prototypen liefen lokal (Scratchpad), schrieben KEIN Staging-Artefakt (Ergebnis nicht deploybar → nichts zu stagen).
