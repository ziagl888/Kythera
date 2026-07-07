# CLAUDE.md — Kythera

Multi-Bot Crypto-Trading-System (Binance Futures) auf einem Windows-VPS. **Hier läuft echtes Geld.** Die Bots handeln über Telegram-Signale → Cornix → Binance. Architektur, Fleet-Übersicht und Setup: `README.md`.

**Vor dem ersten Edit lesen: `docs/OPUS-HANDOFF.md`** — Operating Manual mit Arbeitszyklus, kuratierten Fallen, Qualitätsbar und Eskalationsregeln. Das geranked Backlog liegt in `docs/T-2026-CU-9050-021-opus-task-audit.md`; das lebende Audit-Ledger ist `AUDIT_TODO.md`.

## Harte Regeln (nicht verhandelbar)

1. **Kein Live-Eingriff aus einer Dev-Session.** Keine Fleet-Restarts, keine Schreib-Queries gegen die Live-DB, kein Überschreiben von Produktions-Modell-Artefakten. Die Build-Maschine hat bewusst KEINE DB-Credentials (`.env` ist ein leerer Stub) — DB-gebundene Arbeit läuft nur in einer VPS-Session (T-2026-CU-9050-011).
2. **Modell-Artefakte nur nach `staging_models/`.** Promotion eines Artefakts in den Repo-Root (= live) ist eine explizite Operator-Entscheidung von Michi, nie Teil eines Trainings-Laufs.
3. **Secrets:** `.env` und `.local/` sind gitignored und enthalten echte Tokens/Channel-IDs (`-100…`) — niemals committen, niemals in Code/Doku hardcoden. gitleaks blockt das; `--no-verify` ist verboten.
4. **Genau EINE Cornix-parsebare Message pro Signal.** Die Info-/HTML-Message darf den Cornix-Block nicht wiederholen (Fleet-weiter Doppel-Trade-Bug, gefixt 2026-07-06).
5. **Nur geschlossene Kerzen analysieren** (Forming-Candle/R1). Ausnahme: reine Preis-Checks in den Monitoren 5/8. Kerzen-Indexierung nie "vereinfachen" ohne die Sortierreihenfolge zu prüfen.
6. **Überarbeitete Modelle posten unter neuem Tag** (ABR2, EPD2, RUB2, MIS2, …) via `model_id` in der Artifact-Meta. Alte Tags nie wiederverwenden.
7. **Feature-Builder sind geteilt** (`core/*_features.py`, Trainer == Serving == Replay). Änderungen dort ändern Modellverhalten auf beiden Seiten — bewusst, aber tragend.
8. **Transaktionen committet der Caller.** `core/signal_post.py` und Cooldown-Helper committen nicht selbst.
9. **Regression-Guard nie stillschweigend refreshen**, um Rot auf Grün zu drehen (`KYTHERA_GOLDEN_REFRESH=1` nur mit dokumentiertem Grund).

## Workflow

- Pro Task: KB-Task (Projekt 9050) + Worktree + Branch `feat/<t-id>`, PR auf `main` (ziagl888-Repo → autonomer Merge-Pfad nach bestandenen Kern-Reviews: z-code-reviewer + z-spec-compliance-review).
- Commits/PRs/Code-Kommentare in Englisch, Author Michael Ziegler.
- CI gated nur ruff/format, mypy, Syntax/Imports, Secret-Regex — **grünes CI ≠ korrekt.** Verhalten verifizieren über `backtest/test_*.py` (standalone, DB-frei) und `python tools/regression_guard/guard.py verify|smoke`.
- Nach jedem Merge: `CHANGELOG.md`-Eintrag (Deutsch, wie bestehende), betroffene `AUDIT_TODO.md`-Checkboxen pflegen, KB-Task-Status aktualisieren.

## Eskalation (stoppen und Michi fragen)

Alles Irreversible oder Geld-Wirksame: Artifact-Promotion/Rollout eines Retrains, Fleet-Restart/Deploy, DB-Migration oder Schema-Änderung an Live-Tabellen, Gate-Flips (`AIM2_LIVE_POSTING`, `NEW_IDEAS_LIVE_POSTING`, Orchestrator-Gating), Parken/Entparken von Bots, `.env`-Änderungen, Dashboard-Exposure. Details und Grenzfälle: `docs/OPUS-HANDOFF.md` §6.
