# Git Initialization & Commit Strategy

Goal: create the repo, import the current state with **meaningful, thematic commits**
instead of a single opaque "initial commit". That keeps later `git blame` readable and
allows selective reverts.

## Phase A: Create the repo on GitHub

1. Log in to GitHub.com → "New repository"
2. Name: e.g. `trading-bots` (or whatever you want)
3. **Visibility: PRIVATE** — non-negotiable!
4. **NO** README, .gitignore, or License added (we do that locally)
5. Create the repo, note the URL (e.g. `git@github.com:deinuser/trading-bots.git`)

## Phase B: Prepare the local repo

Assuming you've unpacked the ZIP to `~/trading-bots/`. Then:

```bash
cd ~/trading-bots

# Einmalig Git konfigurieren falls noch nicht geschehen
git config --global user.name "Dein Name"
git config --global user.email "dein@email.com"

# Git initialisieren
git init
git branch -m main
```

## Phase C: The .gitignore and setup files FIRST

**Critical: before you run `git add .`, the .gitignore must be in place!**
Otherwise you'll accidentally commit `.env`, models, state files, etc.

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

## Phase D: Commits in a sensible order

### Option 1: "Realistic history" (recommended)

You commit the 6 batches as individual commits — as if you'd gone through them one after another. That makes the reports meaningful and future reviews easier.

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

### Option 2: "All at once" (simpler, less meaningful)

If you want it quick:

```bash
git add .
git commit -m "initial: Import nach Deep-Review (57 Fixes)

Siehe reports/CHANGELOG.md für Details."
```

Downside: `git blame` shows only this one commit for every problem. You lose the thematic history.

## Phase E: Push to the remote

```bash
# Verbindung zum GitHub-Repo herstellen
git remote add origin git@github.com:deinuser/trading-bots.git

# Erste Push — setzt den upstream-Tracking
git push -u origin main
```

If SSH isn't configured: use the https URL (`https://github.com/deinuser/trading-bots.git`) and authenticate with a Personal Access Token.

## Phase F: Verify

After the first push:

1. Open the repo on GitHub.com
2. **Check the commit history** — should show the 9 thematic commits
3. **Open the Actions tab** — the syntax check should run green
4. **Enable Security → Secret Scanning** (GitHub finds accidentally committed API keys)
5. **Check the files** — no `.env`, no `.pkl`, no `*_state.json` should be visible

## Phase G: For the next iteration with me

Next time you can just give me the repo URL (if you grant me access — there are ways via deploy keys or a read-only collaborator account). Or you clone locally, selectively pack a ZIP and send it to me. The former is much more efficient.

## Troubleshooting

### "I accidentally committed .env"
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

### "Models are too large"
- Git rejects commits >100MB
- Either set up Git LFS (`git lfs install && git lfs track "*.pkl"`)
- Or move models out of the working directory into a separate folder (`~/trading-models/`)

### "Syntax check fails"
Test locally before pushing:
```bash
find . -name "*.py" -not -path "./venv/*" -exec python -c "import ast; ast.parse(open('{}').read())" \;
```

### "I want to revert a single fix"
```bash
git log --oneline               # Commit-Hash finden
git revert <commit-hash>        # Revert-Commit anlegen
git push
```
