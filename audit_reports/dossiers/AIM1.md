# Dossier: AIM1 — Master-/Meta-Modell (Bot 15)

> Stacking-Meta-Modell über alle Bot-Signale (Marktkontext × Schwarm × Quell-Identität). **Note F (Report 16).** Kernverdikt: **aktiv schädlich, verlässlich invertiert** — conf>0,9 → 9,3% WR, Σ −3.399 netto; größter AI-Verlustbringer. **Sofort pausieren** (Report 13/14/16 einstimmig).

> **ABSCHLUSS 2026-07-05:** Operator-Entscheidung — AIM1 wird ad acta gelegt (kein Retrain). Nachfolger **AIM2** nach `docs/AIM2_DESIGN.md` (S7-Bauplan, Batch-E-Gerüst); Slot 15, Channel und Posting-Flow bleiben, `ai_signals.model='AIM2'` für saubere Attribution. Dieses Dossier ist damit historisch.

## 1. Steckbrief

| Feld | Inhalt |
|---|---|
| Bot-Datei | `15_ai_master_bot.py` (scannt Signal-Kandidaten aller Bots, 5-min-Fenster) |
| Artefakt | `master_trade_model_xgboost_combined_signals.pkl` (XGBoost; Feature-Liste per pkl-String-Extraktion verifiziert, Offsets 1884001–1884326) |
| Trainer | `legacy_trainers/x10-mlzeitfolge-v2.py` (`master_task.py` ist nur ein Loader-Prototyp) — Provenienz in Report 13 geklärt |
| Trainingsdatum | nicht dokumentiert; das Dummy-Vokabular beweist: **vor der heutigen Flotte** (kennt nur `ai_model_MSI1-*`-Typos, `conv_bot_{5% Bot, Fast Bot, …}`) |
| Datenquelle | historische Signal-/Indikator-DB-Werte von damals; Feature-Join per `dt.round('1h')` — **rundet AUF** → Join-Kerzen-Close bis ~90 min in der **Zukunft** des Signals (Live nutzt floor) |
| Label-Definition | +10% binnen 72h **vor** −7,5%-SL (Close-basiert) → belohnt Volatilität; pkl-Beweis: Top-Gains `atr_21_pct_close` (137) + `atr_14_pct_close` (97) → **das Modell ist ein Volatilitäts-Detektor** |
| Features | Marktkontext (dist-/ATR-Features), Signal-Schwarm-Kontext, Quell-Identität als One-Hots (Identity-Block = 14,6% des Gesamt-Gains, `conv_bot_nan` drittwichtigstes Feature) — live via `reindex(fill_value=0)` **alle Identity-Dummies = 0** |
| Thresholds/Betrieb | postet fast nur conf>0,85; `scale_pos_weight=2.105`, Testset = Early-Stopping-Set, **keine Kalibrierung**; drei inkonsistente Confidence-Mappings (v2 / master_task / Bot 15) |
| Channel | eigener AIM1-Channel (Step 2: „AIM1-Channel ist aktiv schädlich") |

## 2. Live-Bilanz (Stand 2026-07-03, aktive Ära, dedupliziert)

- **n = 3.047 · WR 50,8% · ø −1,02%/Trade · Median −1,01 · Σ netto −3.399 Preis-%** (Report 14). *Widerspruch beider Stände:* Step-2-Tabelle nennt **n=3.125 / WR 50,3%** (vor Dedup 3.125→3.047 — einziges Modell der aktiven Ära mit nennenswerten Duplikaten); Feb-Start mit 24% WR.
- **Kalibrierung invertiert (Step 2, n=19.561 Shadow+Posted):** corr(confidence, win) = **−0,304**; Bucket 0,8–0,9 → 31,1% WR; Bucket **0,9–1,0 → 9,3% WR**. Report 15 (E5, feinere Buckets, n=19.295): conf 0,9–0,95 → **8,3% WR, −9,53%/Trade**; conf **>0,95 kippt auf 85% WR** (n=267) — die Inversion ist nicht monoton.
- Richtungssplit: nicht ausgewiesen. Shadow-Flut: ~25k ungepostete `ml_predictions_master`-Rows/7d (Step 2).[^1]

[^1]: **Monitor-Vorbehalt (Report 17):** Alle Zahlen monitor-generiert; First-Touch-Replay stimmt nur zu 63,4% mit dem Monitor überein (17,8% verpasste TP1, 18,8% TP1 trotz SL-zuerst); AI-Replay rückwirkend unmöglich (N4: `ai_signals` löscht SL/Targets beim Close). Dazu P1.2/P2.7/P2.31/P1.9. Die AIM1-Inversion ist davon unberührt robust (Vorzeichen + Code-Ursachen belegt).

## 3. Befunde (konsolidiert)

Status: ✔ = bewiesen/bestätigt (Step 2/3) · ✘ = widerlegt/ausgeschlossen · ~ = Code-Befund, offen

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P0.13 | Bot+Modell | P0 | Source-Identity-One-Hots für fast alle Live-Signale tot: pkl kennt nur MSI1-Typos/alte conv-Namen; Live-Overlap 2/22 (`ATS1`,`EPD1`) bzw. **0/5** conv → `reindex` nullt alles, Meta-Modell kann Quellen nicht unterscheiden (sein Kernjob) → OOD | ✔✔ (Step 2+3) |
| — (Step 2) | Modell | P0 | Kalibrierung invertiert: corr −0,304, conf>0,9 → 9,3% WR → Bot pausieren | ✔✔ |
| R13-AIM1-1 | Trainer | P0/P1 | `v2:398`: Feature-Join per `dt.round('1h')` rundet AUF → Feature-Kerze bis ~90 min in der Zukunft; Live nutzt floor → gelernte Richtungen kippen | ✔ (Step 3) |
| R13-AIM1-2 (X-R1) | Trainer | P1 | Volatilitäts-Label (+10%/72h vor −7,5%-SL): volatilste Kandidaten reißen live zuerst den SL → **echte, ehrlich gelernte Inversion** | ✔ (Step 3, pkl-Beweis) |
| R13-AIM1-3 (X-R4) | Trainer | P1 | Keine Kalibrierung, `scale_pos_weight=2.105`, Testset = Early-Stopping-Set | ✔ (Step 3) |
| P1.21 | Bot | P1 | Indikator-Features + Close aus der noch laufenden Stunde (`open_time <= floor('h')`, Features auf 2–34 min Daten); Fix = ein Zeichen (`<`) | ~ (R1 live bewiesen) |
| R13-AIM1-4 („F6", Selbst-Feedback) | Bot | P2 | Hist-Query ohne `model_name`-Filter liest AIM1s **eigene Shadow-Rows** als Input-Signale → Selbst-Feedback-Schleife | ✔ code-belegt (Step 3 / Report 07) |
| P2.35 | Bot | P2 | 5-min-Kandidatenfenster ohne Catch-up (Kommentar sagt 30) + Kontext-Features zählen den Kandidaten selbst + `conv_signal`-Dedup-Key kollidiert über active/closed-Tabellen | ~ |
| R07-AIM1-a | Bot | MEDIUM | Naive Detector-Timestamps als UTC interpretiert; naive `join_time` vs timestamptz | ~ (R3 live bewiesen) |
| R07-AIM1-b | Bot | LOW | Dup-Gate hängt an Monitor-Deletions, kein Age-Cap; conn außerhalb try; Modell wird nie neu geladen | ~ |
| R07-AIM1-c | Bot | P3 | In-Code-„FIX"-Kommentar doppelt falsch (reindex kann nicht shiften; MIS1-Rename kann MSI1-Features nicht wiederbeleben) | ✔ |
| R13-AIM1-5 | Modell | — | **Ausgeschlossen** als Ursachen: Label-Inversion (1=Win verifiziert) und falscher predict_proba-Index (`classes_=[0,1]`, Bot nimmt `[0][1]`) | ✘ |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1/R3 (Step 2 bewiesen):** P1.21 (Forming-Hour-Features) und der TZ-Mix sind live relevant; AIM1 gehört zu den Bots ohne Forming-Candle-Verteidigung.
- **X-R1…X-R6:** AIM1 verletzt X-R1 (Vol-Label), X-R2/X-R4 (Threshold/keine Kalibrierung), X-R6 (Forming-Serving); dazu einzigartig: totes Identity-Vokabular + Join-Lookahead.
- **Selbst-Feedback (F6):** AIM1s Shadow-Output fließt in die eigenen Inputs zurück — jede Vokabular-/Verhaltensänderung anderer Bots UND AIM1s selbst verschiebt die Feature-Verteilung (fragilste denkbare Kodierung: One-Hots über frei benannte Bot-Namen, Report 16).
- **Whitelist/Orchestrator:** Report 16 §7 — ein Teil des ROM1-Mehrwerts (+8pp) ist schlicht Negativselektion der schlimmsten Quellen, allen voran AIM1; nach AIM1-Pause die Gate-Statistik neu bewerten.
- **Datenhygiene:** AIM1 ist das einzige aktive Modell mit Dedup-Delta (3.125→3.047, Report 14 A.1) — Trainingslabels aus `closed_ai_signals` erst nach Purge ziehen (V2 in Report 15).

## 5. Sanierungsplan

**(a) Sofort ohne Retrain: PAUSIEREN.** So wörtlich in Report 13 (Maßnahme 1: „AIM1 pausieren — Step-2-Beweis: invers prädiktiv"), Report 14 (D.3 „Stoppen/parken: AIM1 (invertiert + −3,4k netto)") und Report 16 (§8 „Stoppen: AIM1 (verlässlich invertiert)"). Kein Bot-Fix macht das Modell brauchbar.

**(b) Retrain-Anforderungen = Neuprojekt „AIM2" (Report 15, S7):** aktuelles Vokabular aus DB-DISTINCT (nicht hardcoded), **floor−1-Join identisch in Trainer und Serving**, Label = First-Touch der echten Order-Geometrie (V3-Simulator), Regime-Features aus `regime_history` (2025 noch nicht vorhanden — offensichtlichster fehlender Prädiktor), Quell-Kalibrierungsscore als Feature, Selbstausschluss (kein AIM1-Input), zeitlicher 3-Wege-Split, Isotonic-Kalibrierung, reindex-Parity-Guard. Rolle: **Ranker/Sizer** über Quellsignale, nicht eigenständiger Trader. **Warnung (Report 13):** Neutraining nur aufs Vokabular reicht NICHT — ohne Label-Fix (X-R1) und floor-Join entsteht wieder ein überkonfidentes Volatilitätsmodell.

**(c) Offene Fragen:** S5 „AIM1-Fade" (Signale 0,85–0,95 invertieren, auf Papier ~+9,5%/Trade) **nur als Shadow-Experiment** — die Inversion ist ein OOD-Artefakt und conf>0,95 gewinnt bereits 85%; realistischer Nutzen als Veto-Feature. AIM1-authored Anteil in 5-Tage-Fenstern (Feedback-Magnitude, Report 07 DB-Frage 9) ungemessen; Richtungssplit nie ausgewertet; Master-Gap (non-AIM1-Rows ohne processed-Eintrag) offen.

## 6. Belege

- `AUDIT_TODO.md` → P0.13 (✔✔ inkl. Pausier-Anweisung), P1.21, P2.35
- `audit_reports/07_ai_bots_b.md` → pkl-Feature-Extraktion, Selbst-Feedback, Fenster/Dedup/TZ-Findings, falscher FIX-Kommentar
- `audit_reports/13_x_ml_trainers.md` → Trainer `x10-mlzeitfolge-v2.py`, round-Join-Lookahead, Vol-Label, Verdikt „aktiv schädlich", Ausschlüsse, Maßnahme „pausieren"
- `audit_reports/14_bot_performance_db.md` → n=3.047, WR 50,8%, ø −1,02%, Σ −3.399; Dedup 3.125→3.047; Empfehlung stoppen
- `audit_reports/STEP2_DB_VERIFICATION.md` → Kalibrierungs-Inversion (corr −0,304; 0,9–1,0 → 9,3% WR, n=19.561), Dummy-Overlap 2/22 bzw. 0/5, WR 50,3% (n=3.125)
- `audit_reports/16_strategy_concept_evaluation.md` → Note F, Konzeptanalyse (Architektur verletzt alle Stacking-Voraussetzungen)
- `audit_reports/15_strategy_proposals.md` → E5-Zahlen, S5 AIM1-Fade (nur Shadow), S7 AIM2-Bauplan
- `audit_reports/17_monitor_replay_and_gaps.md` → Monitor-Vorbehalt, N4 (AI-Replay unmöglich)
