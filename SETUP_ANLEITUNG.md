# Git-Initialisierung & Commit-Strategie

Ziel: Repo erstellen, den aktuellen Stand mit **sinnvollen, thematischen Commits** importieren
statt einem einzigen opaken "initial commit". Das macht späteres `git blame` lesbar und
erlaubt selektive Reverts.

## Phase A: Repo auf GitHub anlegen

1. Auf GitHub.com einloggen → "New repository"
2. Name: z.B. `trading-bots` (oder was du willst)
3. **Visibility: PRIVATE** — unbedingt!
4. **KEINE** README, .gitignore oder License hinzufügen (machen wir lokal)
5. Repo erstellen, URL merken (z.B. `git@github.com:deinuser/trading-bots.git`)

## Phase B: Lokales Repo vorbereiten

Angenommen du hast das ZIP nach `~/trading-bots/` ausgepackt. Dann:

```bash
cd ~/trading-bots

# Einmalig Git konfigurieren falls noch nicht geschehen
git config --global user.name "Dein Name"
git config --global user.email "dein@email.com"

# Git initialisieren
git init
git branch -m main
```

## Phase C: Die .gitignore und Setup-Files ZUERST

**Kritisch: Bevor du `git add .` machst, muss die .gitignore stehen!**
Sonst committest du versehentlich `.env`, Modelle, State-Files usw.

```bash
# Die Setup-Dateien aus dem github_setup-Ordner kopieren (falls du sie nicht schon im ZIP hast):
cp /pfad/zu/github_setup/.gitignore .
cp /pfad/zu/github_setup/README.md .
mkdir -p .github/workflows
cp /pfad/zu/github_setup/.github/workflows/syntax-check.yml .github/workflows/

# Verifizieren was ignoriert wird:
git status --ignored
# Ausgabe sollte .env, *.pkl, logs/, state/*.json usw. als "ignored" zeigen

# Wenn etwas Sensibles doch auftaucht (NICHT ignoriert wird), .gitignore anpassen
# BEVOR du committest!
```

## Phase D: Commits in sinnvoller Reihenfolge

### Option 1: "Realistische Historie" (empfohlen)

Du committest die 6 Batches als einzelne Commits — so als wärst du sie nacheinander durchgegangen. Das macht die Reports aussagekräftig und zukünftige Reviews einfacher.

```bash
# 1. Initial mit Setup-Dateien + grundlegender Projektstruktur OHNE Code
git add .gitignore README.md .github/
git commit -m "chore: initial repo setup

- .gitignore (secrets, models, state-files, runtime-output)
- README mit Architektur-Übersicht
- GitHub Action für Syntax-Check"

# 2. Pre-Review-Stand (hypothetisch)
# Falls du einen "vorher"-Stand hast (dein Original-ZIP): den committen, danach die Fixes.
# Falls nicht: direkt Schritt 3.

# 3. Batch 1: Data Ingestion, Monitor & Housekeeping
git add 1_data_ingestion.py 5_trade_monitor.py 6_housekeeping.py \
        7_pattern_detector.py 8_ai_trade_monitor.py
git commit -m "fix(batch-1): DB-Robustheit & State-Persistence

- #8/#16 Monitor auto-reconnect bei DB-Hiccup (5_trade_monitor, 8_ai_trade_monitor)
- #14 DB-Flusher SAVEPOINT-basiert (keine Kaskaden-Rollbacks)
- #21 active_patterns.json atomares Write via tmp+fsync+os.replace
- #36 targets_hit defensiv zu int gecastet
- #48 telegram_outbox Nightly Cleanup (7 Tage)

Siehe reports/batch_1_report.md"

# 4. Batch 2: AI-Bot Signal-Quality
git add 10_pump_dump_detector.py 11_ai_mis_bot.py 12_ai_ats_bot.py \
        13_ai_rub_bot.py 14_ai_atb_bot.py 18_ai_abr1_bot.py
git commit -m "fix(batch-2): AI-Bot Feature-Robustheit & Cooldown-Reihenfolge

- #17 RUB Cooldown-Check VOR ML-Prediction (CPU-Einsparung)
- #20 ATB NaN/Inf-Absicherung vor predict_proba
- #24 RUB get_f handled NaN/Inf nicht nur None
- #25 ABR1 X_event defensive NaN/Inf-Clean
- #27 MIS1 Thresholds beim Load geloggt (Drift-Detection)
- #74 ABR1 SUCCESS_CLASS_IDX=0 mit WARNING markiert (Review vor Deploy!)
- #75 ABR1 asymmetrische Thresholds dokumentiert
- #76 ABR1 redundanter minute-Filter entfernt

Siehe reports/batch_2_report.md"

# 5. Batch 3: Cooldown-Konsolidierung
git add 14_ai_atb_bot.py 15_ai_master_bot.py 16_smc_forex_metals_bot.py \
        core/market_utils.py
git commit -m "refactor(batch-3): Cooldown-Duplikate entfernt, zentrale market_utils

- #33 SMC Forex is_cooled_down (vermischter Check+Update mit Seiteneffekt) → market_utils
- #34 SMC Forex Cooldown-Keys ohne TF-Suffix (1h/4h TF-übergreifend)
- #51 ATB eigenes is_cooled_down/set_cooldown → check_cooldown/update_cooldown
- #28 Master Bot symbol-cleanup-Regex robuster

Siehe reports/batch_3_report.md"

# 6. Batch 4: Indicator Engine & Strategies
git add 2_indicator_engine.py strategies/
git commit -m "fix(batch-4): Indicator Engine NaN-Robustheit

- #6 Trendline: Division durch 0 + NaN bei konstanten Preisen abgefangen
- #12 Volume Indicator: df.loc[index-1] → iloc mit reset_index
- #45 indicator_state.json atomares Write

7 ursprüngliche Punkte als Fehlalarm geklärt (HVN-Binning, BB-std, KAMA etc.)
Siehe reports/batch_4_report.md"

# 7. Batch 5: Market Tracker, Whale & Funding Logger
git add 19_whale_logger_bot.py 20_funding_logger_bot.py 23_market_tracker.py \
        core/update_model.py
git commit -m "fix(batch-5): Market Tracker, Whale & Funding Logger

- #71/#73 Market Tracker Kategorie-Mapping (TD/BB/QM als PATTERN)
- #72 Volume-Näherung via mid-price statt close-only
- #81 format_usd handled negative Werte (-\$1.5M)
- #82 check_top20_positive_pct returns None bei leeren Daten (nicht 50.0)
- #83 calc_diff_bps returns None, Display zeigt 'N/A'
- #85 update_model skippt Threshold-Files explizit

Siehe reports/batch_5_report.md"

# 8. Batch 6: Architektur, Charting, Dashboard
git add 4_telegram_bot.py 6_housekeeping.py main_watchdog.py \
        9_ai_sr_bot.py 10_pump_dump_detector.py 12_ai_ats_bot.py \
        13_ai_rub_bot.py 14_ai_atb_bot.py core/trade_utils.py core/state_utils.py
git commit -m "refactor(batch-6): Code-Zentralisierung & Chart-Referenz-Handling

- #52 get_hvn_and_sr_levels zentralisiert (5 bit-identische Kopien → core.trade_utils)
- #68/#87 Telegram: Chart nur löschen wenn keine anderen ungesendeten Refs
- #31 Housekeeping respektiert Outbox-Referenzen beim Chart-Cleanup
- #70 Dashboard-Output in logs/dashboard.log statt DEVNULL
- #88 core/state_utils.py neu: atomic_write_json + atomic_read_json

Siehe reports/batch_6_report.md"

# 9. Reports + CHANGELOG
git add reports/
git commit -m "docs: Deep-Review Reports & CHANGELOG

Dokumentation der kompletten Review-Runde:
- 91 Analyse-Punkte geprüft
- 57 echte Bugs behoben
- 20 Fehlalarme geklärt
- 6 Punkte explizit out-of-scope
- 5 als zu invasiv dokumentiert
- 3 asyncio-unkritisch

Offene Punkte für späteren Review in CHANGELOG.md"

# 10. Pre-Batch-Phase-Fixes (26 Fixes die vor den strukturierten Batches kamen)
# Falls du die separat darstellen willst: zurück zu Schritt 2 und zwei separate Commits machen.
# Für den Start: alles als "Batch 0" zusammenfassen oder ignorieren.
```

### Option 2: "Alles auf einmal" (einfacher, weniger aussagekräftig)

Wenn du es schnell haben willst:

```bash
git add .
git commit -m "initial: Import nach Deep-Review (57 Fixes)

Siehe reports/CHANGELOG.md für Details."
```

Nachteil: `git blame` zeigt bei jedem Problem nur diesen einen Commit. Du verlierst die thematische Historie.

## Phase E: Remote pushen

```bash
# Verbindung zum GitHub-Repo herstellen
git remote add origin git@github.com:deinuser/trading-bots.git

# Erste Push — setzt den upstream-Tracking
git push -u origin main
```

Falls SSH nicht konfiguriert ist: https-URL verwenden (`https://github.com/deinuser/trading-bots.git`) und mit Personal Access Token authentifizieren.

## Phase F: Verifizieren

Nach dem ersten Push:

1. Auf GitHub.com das Repo öffnen
2. **Commit-History prüfen** — sollte die 9 thematischen Commits zeigen
3. **Actions-Tab öffnen** — Syntax-Check sollte grün laufen
4. **Security → Secret Scanning aktivieren** (GitHub findet versehentlich committete API-Keys)
5. **Files prüfen** — KEIN `.env`, KEIN `.pkl`, KEIN `*_state.json` darf zu sehen sein

## Phase G: Für die nächste Iteration mit mir

Beim nächsten Mal kannst du mir einfach die Repo-URL geben (falls du mir Zugriff gibst — gibt's Wege über Deploy-Keys oder ein Read-Only-Collaborator-Account). Oder du clonst lokal, packst selektiv ein ZIP und schickst es mir. Ersteres ist deutlich effizienter.

## Troubleshooting

### "Ich habe versehentlich .env committed"
```bash
# Sofort lokal entfernen
git rm --cached .env
echo ".env" >> .gitignore
git commit -m "chore: entferne versehentlich committete .env"
git push

# WICHTIG: API-Keys die in der .env standen sofort rotieren!
# Der Commit bleibt in der Git-Historie sichtbar, selbst nach dem Entfernen.
# Für echtes Entfernen: git filter-repo oder BFG — aber Keys rotieren ist einfacher.
```

### "Modelle sind zu groß"
- Git lehnt Commits >100MB ab
- Entweder Git LFS einrichten (`git lfs install && git lfs track "*.pkl"`)
- Oder Modelle aus dem Arbeitsverzeichnis in einen separaten Ordner (`~/trading-models/`) verschieben

### "Syntax-Check schlägt fehl"
Lokal vor Push testen:
```bash
find . -name "*.py" -not -path "./venv/*" -exec python -c "import ast; ast.parse(open('{}').read())" \;
```

### "Ich will einen Fix einzeln rückgängig machen"
```bash
git log --oneline               # Commit-Hash finden
git revert <commit-hash>        # Revert-Commit anlegen
git push
```
