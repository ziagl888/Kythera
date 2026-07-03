# Dossier: IP / Pattern-Detector (Bots 7 + 22)

> Chartbasierte Pattern-Signalgeber: `7_pattern_detector.py` erzeugt u.a. die Break-&-Retest-Familie **BR1H/BR2H/BR4H (+BR1D)** — ohne ML-Gate; `22_ip_pattern_bot.py` ist in den Audit-Quellen fast unsichtbar. **Note (16): BR-Familie D.** Kernverdikt: Break-and-Retest roh, mit 4-fachem Signalvolumen der ML-Schwester BB und **Σ −4.106 netto** — der direkte Vergleich BB_4H (+ML, +565) vs. BR (ohne ML, −4.106) ist das beste In-vivo-Argument im Repo für ein ML-Gate; Sofortmaßnahme: BR1H-SHORT-Seite schließen.

## 1. Steckbrief

| | |
|---|---|
| Bots | `7_pattern_detector.py` + `22_ip_pattern_bot.py` — chartbasiert, kein ML |
| Signale/TF | **BR1H / BR2H / BR4H** (Report 14) + **BR1D** (Step-2-Zeile „BR1H/2H/4H/1D"). **Quellen-Klärung:** Report 16, Abschnitt 6 („im Code verifiziert"): `BR1H/2H/4H` = Break-and-Retest **ohne** ML **aus dem Pattern-Detector (7, nicht aus 25!)** — die BR-Familie gehört also zu Bot 7, nicht zum SMC-ML-Sniper |
| Rolle | Report 16, Abschnitt 7: „Pattern-Detector 7 ist kein Intelligence-, sondern ein (netto negativer) Signal-Layer" |
| Bot 22 | in den gelesenen Quellen nur über P3.11 (Chart-Verzeichnis-Growth) erwähnt; keine Signale/Tags, keine Performance-Daten, keine eigene Note — Abdeckungslücke |
| Leverage | in den Quellen nicht beziffert |

## 2. Live-Bilanz (aktive Ära 24.02.–03.07., dedupliziert)

| Familie | n | WR | ø PnL | Median | Σ netto |
|---|---|---|---|---|---|
| BR4H / BR2H / BR1H (Report 14) | 11.756 | 58–60% | −0,1…−0,3% | ≈0 | **−4.106** |
| BR1H/2H/4H/1D (Step-2-Zählung) | 12.034 | 57–60% | — | — | — |

- **Richtungs-Asymmetrie (E1):** BR1H **LONG 65,5% vs. SHORT 49,5% WR** — die SHORT-Seite zieht die Familie herunter; Report 15/S1: „BR1H nur LONG".
- Regime-Drift: BR-/BB-Familie Mär–Apr stark negativ, ab Mai positiv (Mini-n, das Regime-Gating filtert sie inzwischen fast weg).
- Keine Kalibrierungsdaten (kein ML → keine Confidence).
- Step 2 (P0.1-Kontext): identische Messages „PatternDetector" 2–3× binnen 60 min in Trading-Channels → Upstream-Doppel-Generierung (Detector-Refire).
- Vorbehalt (Report 17): monitor-generiert, nur 63,4% Replay-Übereinstimmung (P1.2/P2.7); für die AI-Flotte ist ein Replay wegen N4 (gelöschte `ai_signals`-Targets) rückwirkend unmöglich.

## 3. Befunde

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| 16-Verdikt | Konzept | HOCH | BR = Break-and-Retest ohne ML-Gate bei 4-fachem Signalvolumen von BB → Σ −4.106; dieselbe Idee mit ML-Gate (BB_4H) ist positiv | ✔ (Report 14/16) |
| E1/S1 | Live | HOCH | BR1H SHORT 49,5% WR vs. LONG 65,5% — SHORT-Seite verlässlich schädlich | ✔ (DB, dedupliziert) |
| Step2-Dup | Bot | MITTEL | Upstream-Doppel-Generierung: md5-identische PatternDetector-Messages 2–3× binnen 60 min in Trading-Channels (Detector-Refire; kein Outbox-Retry-Doppel) | ✔ (Step 2) |
| P3.11 | Infra | LOW | Chart-Verzeichnisse wachsen unbegrenzt (`7:27-28`, `22:29-30`) — prüfen, ob Housekeeping genau diese Dirs räumt | ~ (ungeprüft) |
| Lücke | Audit | — | Bot 22 (`22_ip_pattern_bot.py`) hat in keiner der Quellen Findings, Tags oder Zahlen — weder bewertet noch entlastet | ~ (offen) |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 (Forming Candle):** flottenweiter Look-ahead-/Repaint-Root-Cause; die BR-Signalerzeugung hängt an denselben `{sym}_{tf}`-Tabellen mit Partial-Kerzen.
- **Monitor-Vorbehalt (Report 17):** BR-WR/PnL sind monitor-generiert (P1.2 Trailing-SL zieht nie nach, P2.7 verpasste Hits) — die per-Trade-Wahrheit ist unzuverlässig, und die Whitelist des Orchestrators gated auf genau diesen Zahlen.
- **Regime-Gating:** BB/BR werden vom Gating inzwischen fast weggefiltert (Report 14) — jede Re-Aktivierung sollte erst nach P0.4-Whitelist-Fix bewertet werden.
- **S1 (Report 15):** BR1H ist expliziter Baustein des „Direction-Gated Portfolio" (BR1H nur LONG).

## 5. Sanierungsplan

**Sofort (kein Retrain, reine Konfig):** BR1H-SHORT-Seite schließen (S1; Report 16 Empfehlung 8.2). BR-Familie insgesamt prüfen/parken (Report 14 D.3: „netto negativ; ggf. nur LONG-Seite bei BR1H behalten"). Detector-Refire-Dedupe (Ursache der Doppel-Messages) angehen.

**Struktur:** Der belegte BB-vs-BR-Kontrast legt nahe, BR-Rohsignale nicht abzuschalten, sondern als **Event-Quelle unter ein ML-Gate** zu stellen (Muster S11 aus Report 15: Meta-Klassifier über großem gelabeltem Signalstrom) — nach V1–V3 (R1-Fix, Dedup-Index, First-Touch-Simulator) und Monitor-Rewrite.

**Offene Fragen:** Was genau macht `22_ip_pattern_bot.py` und erzeugt es eigene Signale/Tags? (In keiner Quelle beantwortet.) Erzeugt Bot 7 neben BR weitere Pattern-Tags? Räumt das Housekeeping die Chart-Verzeichnisse (P3.11)? BR1D-Zahlen separat ausweisen (Step 2 zählt es mit, Report 14 nicht).

## 6. Belege

- `AUDIT_TODO.md` P3.11 (+P0.1-Annotation zur Upstream-Doppel-Generierung)
- `audit_reports/14_bot_performance_db.md` (BR-Zeile: n=11.756, Σ −4.106, BR1H LONG 65,5%/SHORT 49,5%)
- `audit_reports/STEP2_DB_VERIFICATION.md` (BR1H/2H/4H/1D n=12.034; PatternDetector-Doppel-Messages)
- `audit_reports/15_strategy_proposals.md` (E1, S1: BR1H nur LONG)
- `audit_reports/16_strategy_concept_evaluation.md` (Tag-Klärung BR→Bot 7; Ranking #17; Abschnitt 7 Intelligence-Layer)
- `audit_reports/17_monitor_replay_and_gaps.md` (Monitor-Vorbehalt, N4)
