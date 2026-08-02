# T-2026-KYT-9050-009 — VPS-Orchestrierung, Audit-Rest: Ist-Messung der Rest-Jobs

**Session:** 2026-08-01/02 auf SRV02 (Live-Host) · **Modus:** read-only gegen Live-DB und Live-Prozesse,
Code-Änderungen nur im Worktree · **Kein** Deploy, **kein** Fleet-Restart, **kein** Gate-Flip,
**keine** Artefakt-Promotion, **keine** Schreib-Query.

Auftrag war die Rest-Kette aus dem Vorgänger-Task (Jobs 7/8/10/11 + Doku-PR). Regel dieser Session:
**jeder Punkt wird erst am heutigen Code und an der heutigen Umgebung nachgemessen, dann bearbeitet.**
Fünf der acht Punkte haben sich dabei gegen ihre Aktenlage gedreht — in beide Richtungen.

---

## Ergebnis in einem Satz je Punkt

| Punkt | Aktenlage vorher | Messung heute | Konsequenz |
|---|---|---|---|
| **P0.7-Rest** | „No-op, die 5 Korrupt-Trades sind weg" | Korrupt-Trades weg **stimmt** — aber die Fehlerklasse ist offen und produziert weiter: 342/3463 SR- und 12/188 MC-Trades seit 01.07. mit TP1 auf der falschen Seite | Root-Cause gefunden und **gefixt** (Code + Test) |
| **P2.2** | Checkbox flippen | `module` ist live `varchar(50)` (ALTER lief) — aber die TZ-Drift steht **weiter** in `26_regime_detector.py` | Checkbox bleibt **offen**, Annotation präzisiert |
| **Query-9 (P2.25)** | „VPS-Follow-up offen" | 1590 Whitelist-Rows, **alle** aus dem letzten Stundenlauf, 0 Rohnamen, 0 stale | ✔ verifiziert |
| **P2.15** | „braucht echtes Listing" | GRVTUSDT: echtes Listing, auf **laufender** Fleet erkannt, Catch-up + aktuelle Kerzen | ✔ verifiziert (mit einem benannten Rest) |
| **Job 7 / B4 / Z2** | „wartet auf Cloudflare-Domain" | Dashboard hängt **jetzt** offen im Internet und wird täglich gescannt; `stop_all` ohne Auth | Domain ist **nicht** der einzige Weg → Michi-Entscheid |
| **Job 8 / Z0** | „VPS-CPU dauerhaft 100%, WICHTIGSTER PUNKT" | 10-min-Sample: 78% Box-Mittel, davon ~34%-Punkte die messende Session selbst | Kennzahl ist so **nicht** belastbar; Messwerkzeug + saubere Messvorschrift geliefert |
| **Job 10 / B7** | „MIS1-400d-Replay + Retrains offen" | MIS2 postet live, ATB2/ATS2 gebaut; offen sind nur **QM + SRA1** | Job in der beauftragten Form **obsolet** |
| **Job 11 / Signal-Raten** | „Deltas ab 13./14.07. messen" | Fenster 3 Wochen alt, >20 Restarts dazwischen, Datenlage retentions- und survivorship-verzerrt | **nicht rekonstruierbar** — geschlossen mit Begründung, nicht mit Zahl |

---

## 1 · P0.7: die Annotation war halb richtig — und deshalb gefährlich

**Was in der Akte stand.** `P0.7 [x] … Offen: die 5 bestehenden aktiven Korrupt-Trades bereinigen`,
und im Vorgänger-Task „AUDIT_TODO-Annotationen (P0.7-**No-op**)".

**Was gemessen wurde.** Die 5 Trades sind tatsächlich weg — `active_trades_master` (558 Rows ab
2026-02-24) enthält **0** Zeilen mit der P0.7-Signatur (TP1 ≈ 0,75·Entry LONG / 1,25·Entry SHORT);
im geschlossenen Archiv gibt es genau **1**, letzte am **2026-05-27**, also vor dem Fix vom 04.07.
Insofern: No-op bestätigt.

**Was die Akte nicht sah.** Die Signatur ist nur eine von zwei Türen in denselben Schaden.
Zählt man die Fehlerklasse statt der Signatur — *TP1 auf der falschen Seite des Entry* —, dann:

| Strategie | Trades seit 01.07. | davon TP1 falschseitig | Anteil |
|---|---|---|---|
| Support Resistance | 3.463 | **342** | 9,9 % |
| Main Channel | 188 | **12** | 6,4 % |
| 5 Percent / Fast In And Out / Volume Indicator | 17.792 | **0** | 0 % |

Neuester Fall: **2026-08-01 23:33 UTC**, also Stunden vor dieser Messung. Ein aktiver Fall stand zum
Messzeitpunkt im Buch (`active_trades_master` id 211171, LABUSDT SHORT, Entry 0,1591, TP1 0,15965 und
TP2 0,16020 **über** dem Entry).

Dass ausschließlich die beiden zonenbasierten Strategien betroffen sind und die drei anderen exakt
null Fälle haben, ist der Fingerabdruck der Ursache.

**Root-Cause.** `find_support_resistance_zones()` filtert seine Zonen gegen den **Close der letzten
geschlossenen Kerze**; die Strategien bauen die Ziel-Leiter aber gegen **`entry = live_price`**. Zwei
verschiedene Bezugspreise — und zwischen ihnen bewegt sich der Markt. Sobald der Live-Preis über eine
Resistance-Zone gelaufen ist, wählt `sorted(zones, key=|zone − entry|)[:4]` genau diese Zone als TP1,
und die nachgelagerte Interpolation (`x = (t1 − entry)/4`) wird negativ und zieht TP2/TP3 mit nach
unten. Der 2026-07-04 eingebaute Guard `if t1 == 0: return None` deckt nur den Fall *gar keine Zonen*
ab — die zweite Tür blieb offen.

**Warum das mehr ist als eine hässliche Leiter.** Ein TP1 auf der Verlustseite wird vom Monitor als
Treffer gewertet:

| Bucket (SR, seit 01.07.) | status ≥ 1 („TP getroffen") |
|---|---|
| sauber (3.121 Trades) | 66,2 % (2.066) |
| TP1 falschseitig (342 Trades) | **96,5 %** (330) |

Diese Trades gehen als Gewinner in die Per-Bot-Statistik, auf der das Orchestrator-Gating
(Bot 27 → 28) entscheidet. Die Fehlerklasse ist damit nicht nur Geometrie, sie ist Messfehler in der
Regelschleife.

**Fix.** Neuer geteilter Helfer `core.market_utils.select_zone_targets(zones, entry, direction)` —
filtert die Zonen gegen **den Preis, gegen den die Leiter gerechnet wird**, sortiert nächstliegend
zuerst. Beide Strategien, beide Richtungen nutzen ihn (4 Stellen). Die Leiter ist damit
monoton in Handelsrichtung; der bestehende `t1 == 0`-Guard deckt jetzt zusätzlich „keine Zone auf der
Gewinnseite" ab. Test: `backtest/test_zone_target_side.py` (8 Fälle, DB-frei, inkl.
LABUSDT-Regression).

**Live-Semantik, gemessen statt behauptet** (Basis: geschlossene Trades 01.07.–01.08.):

| Strategie | Leiter ändert sich | Signal entfällt komplett |
|---|---|---|
| Support Resistance | 350 / 3.463 = 10,1 % | 37 / 3.463 = **1,1 %** |
| Main Channel | 12 / 188 = 6,4 % | 4 / 188 = **2,1 %** |

Einschränkung dieser Schätzung, damit sie niemand für exakt hält: die DB zeigt die Leiter **nach**
der Interpolation, nicht die rohe Zonenliste. „Leiter ändert sich" ist deshalb eine **Untergrenze**
(eine falschseitige Zone, die `[:4]` ohnehin abgeschnitten hat, ist unsichtbar); „Signal entfällt" ist
belastbar, weil dort alle Ziele falschseitig sind. Wirksam wird der Fix erst mit dem nächsten
Fleet-Restart — das ist Michis Moment, nicht der des PRs.

## 2 · Job 7 / B4 / Z2 — die Blockade ist nicht die, die in der Akte steht

Aktenlage: „wartet auf Cloudflare-Domain von Michi". Gemessen am 2026-08-02:

- `dashboard.py` bindet unverändert `0.0.0.0:5000`.
- `cloudflared` ist **nicht** installiert (kein Binary, kein Dienst) — die Domain ist also nicht der
  einzige fehlende Baustein.
- `netstat` zeigt zum Messzeitpunkt eine **ESTABLISHED-Verbindung von einer fremden öffentlichen IP**
  auf die öffentliche Adresse des VPS, Port 5000. Wer das war, ist nicht identifiziert und auch nicht
  der Punkt.
- `logs/dashboard.log` belegt laufende Internet-Scans: `GET / HTTP/1.1" 200` an 66.132.172.102,
  Exploit-Pfade wie `GET /v404/exec?jwt=…` von 34.79.154.21, TLS-Handshakes gegen den HTTP-Port,
  `POST /` und `POST /mcp` von wechselnden IPs. Die Dashboard-Seite wird an Fremde ausgeliefert.
- `grep -i auth dashboard.py` → **kein Treffer**. Erreichbar sind u. a.
  `POST /api/system/stop_all`, `/api/system/restart_all` und `/api/process/<script>/stop`.

In der bisher gesichteten Log-Historie ist **kein** Fremdzugriff auf einen Control-Endpoint zu sehen
— die Scans blieben auf `GET /`. Das ist Glück, keine Schutzmaßnahme.

**Geliefert (ohne Verhaltensänderung):** `dashboard.py` liest die Bind-Adresse jetzt aus
`DASHBOARD_BIND_HOST`, **Default bleibt `0.0.0.0`** — ein stilles Umstellen hätte Michi beim nächsten
Restart die Fernsicht abgeschnitten. Zusätzlich eine Warnzeile beim Start, solange nicht auf Loopback
gebunden wird, und ein dokumentierter Eintrag in `.env.example`.

**Offener Entscheid (Michi) → siehe §7, Entscheid 1.**

## 3 · Job 8 / Z0 — die 100 % halten der Messung nicht stand, aber die Messung sich selbst auch nicht

Werkzeug: `tools/ops/measure_cpu_baseline.ps1` (read-only, WMI-Perf-Counter statt kumulativer
`Get-Process .CPU`-Sekunden — die Falle vom 2026-07-20).

Lauf 2026-08-02 00:27–00:37, 35 Samples, 10 logische Kerne: **Box-Mittel 78 %** (nicht 100 %; der
100er-Wert stammt aus 3-Sekunden-Stichproben).

| Posten | % der Box | Einordnung |
|---|---|---|
| python (Fleet-Bots, 41 PIDs direkt unter dem Watchdog) | 18,5 | Fleet |
| python (übrige, v. a. Indicator-Engine-Pool-Worker) | 10,6 | Fleet |
| postgres (120 verschiedene PIDs im Fenster) | 14,1 | DB |
| ccSvcHst (Symantec) | 5,5 | AV |
| System | 2,7 | OS |
| **claude / bash / powershell / conhost / git / Taskmgr / WmiPrvSE / py / python3** | **≈ 34** | **die messende Session selbst** |

**Der wichtigste Befund ist der Beobachtereffekt.** Rund 34 Prozentpunkte der 78 % gehen auf die
Agent-Session und ihr Werkzeug — allein `claude` 16,4 %, der Sampler (WmiPrvSE) 4,4 %. Zieht man das
ab, liegt die Grundlast von Fleet + DB + AV bei **≈ 48–50 %**, also am Z0-Ziel („<50 %") statt bei
„dauerhaft 100 %". Diese Zahl ist eine **Subtraktion, keine Messung** — sie taugt als Hinweis, nicht
als Abnahme.

Zwei Nebenbeobachtungen aus denselben Daten: 120 verschiedene postgres-PIDs in 10 Minuten (Connection-
Churn, passt zum `_POOL_MIN`-Kommentar in `core/database.py`) und 142 kurzlebige `py`/`python3`-PIDs
(~14 Neustarts/Minute), deren Elternprozess zum Snapshot-Zeitpunkt schon weg war — **nicht
attribuiert**, bewusst nicht geraten.

Per-Bot-Attribution war in dieser Session nicht möglich: `Win32_Process.CommandLine` liefert für die
elevated laufende Fleet `$null`, und der Watchdog loggt nur für das Dashboard eine PID.

**Empfehlung:** den Sampler ohne Agent-Session laufen lassen (geplante Aufgabe zu ruhiger Stunde),
erst dann ist Z0 abnahmefähig. Kein Fix aus dieser Messung abgeleitet — „erst messen, dann fixen"
heißt auch: nicht auf einer kontaminierten Messung fixen.

## 4 · Job 10 / B7 — in der beauftragten Form erledigt

- **MIS1-400d-Replay + Retrain:** ausgeführt. `mis2_model_{8,24,72,168}h_{pump,dump}.pkl` liegen im
  Repo-Root (= live), und die Modelle posten: `ai_signals` führt offene MIS2-Signale mit
  Zeitstempeln bis 2026-08-01.
- **ATB1 → ATB2, ATS1 → ATS2:** gebaut (Artefakte in `staging_models/` und Root).
- **Adapter-Stand `tools/walkforward_sim.py:1151`:** `ufi1, td, bb, abr1, mis1, rub, atb2, ats`;
  `tools/retrain_from_replay.py:1054` zusätzlich `epd`.
- **Wirklich offen:** **QM** und **SRA1** haben keinen Walk-Forward-Adapter. SRA2 existiert, läuft
  aber über den Meta-Labeling-Pfad (`tools/retrain_sra2.py`, `closed_trades3`), nicht über den
  gemeinsamen Simulator.

Job 10 als „MIS1-Replay hinter der Job-Queue" ist damit gegenstandslos. Der Rest ist ein eigener,
kleinerer Task (2 Adapter) und braucht keine VPS-Session.

## 5 · Job 11 — eine Hälfte verifiziert, die andere nicht mehr rekonstruierbar

**P2.15 („braucht echtes Listing") — verifiziert.** GRVTUSDT ist das erste echte Listing seit dem Fix:

- `logs/DATA_INGESTION.log`, 2026-08-01 06:01:38: `🆕 1 neue Coins in coins.json: GRVTUSDT` — der
  letzte Fleet-Restart davor war am **2026-07-30 07:25**, der nächste am 2026-08-01 19:33. Der
  additive Pfad hat also auf **laufender** Fleet gegriffen.
- `candles`: GRVTUSDT/1h von 2026-07-31 15:00 bis 2026-08-02 00:00, 34 Kerzen — Catch-up **vor** dem
  Erkennungszeitpunkt und laufende Fortschreibung.
- `GRVTUSDT_1h` existiert und ist leer. Das ist **kein** P2.15-Defekt, sondern der C-Gate-Zustand:
  alle Legacy-Per-Coin-Tabellen enden bei 2026-07-16 (T-2026-KYT-9050-002).
- **Rest, benannt:** die erste `ticker_10s`-Zeile für GRVTUSDT steht erst um 19:36:50, also nach dem
  Restart. Schreiber ist `10_pump_dump_detector.py`, das seine Coin-Liste weiterhin beim Start
  einfriert — Bot 10 war nie Teil des P2.15-Scopes (der deckte `1_data_ingestion` und
  `chart_data_service` ab). Kein Regress, aber die Lücke ist jetzt belegt statt vermutet.

**Signal-Raten-Deltas Post-Restart — geschlossen als nicht rekonstruierbar.** Der Auftrag zielte auf
24–48 h nach dem Restart vom 2026-07-12, also auf den 13./14.07. Dazwischen liegen drei Wochen und
über zwanzig Restarts. Erschwerend, und der eigentliche Grund für das Nein:

- `ai_signals` ist das **offene** Buch; eine Zählung „pro Tag" misst dort, wie viele Signale eines
  Tages noch offen sind, nicht wie viele entstanden — der scheinbare Anstieg 102 → 1.061 zwischen
  27.07. und 01.08. ist reiner Survivorship.
- Eine deduplizierte Vereinigung mit `closed_ai_signals` liefert ~2.000/Tag, ist aber für ältere Tage
  durch Retention nach unten verzerrt (12.–14.07.: 937/494/346) und damit **nicht** über das Fenster
  vergleichbar.

Belastbar ist nur die klassische Seite (geschlossenes Archiv, keine Retention im Fenster):
`closed_trades_master` liegt stabil bei **~700–900 Signalen/Tag** über die letzten 21 Tage, ohne
erkennbaren Bruch. Eine Zahl für das ursprüngliche Delta wird hier **nicht** erfunden.

## 6 · Doku-Rest + zwei Orchestrierungs-Befunde

**P2.2 — Checkbox bleibt offen.** Live ist `trade_cooldowns.module` heute `character varying(50)` und
`last_posted_at` `timestamp with time zone` (per `information_schema` verifiziert). Der ALTER ist also
gelaufen (laut Vorgänger-Task am 2026-07-12 freigegeben — von dieser Session **nicht** beobachtet, nur
sein Ergebnis) und im CHANGELOG bisher nirgends vermerkt; das ist jetzt nachgeholt. Der eigentliche
P2.2-Kern ist aber **nicht** zu: `26_regime_detector.py:242` legt `trade_cooldowns` weiterhin mit
`module TEXT` und `last_posted_at TIMESTAMP WITHOUT TIME ZONE` an, während 11/24/25/30 `VARCHAR(50)` +
`WITH TIME ZONE` sagen. Auf einer frischen DB entscheidet also weiter die Bootstrap-Reihenfolge über
die Cooldown-Semantik. Checkbox bleibt `[ ]`, Annotation präzisiert.

Nachtrag dazu: `COOLDOWN_MODULE_MAX_LEN = 10` in `core/market_utils.py` begründet sich im Kommentar
mit „die Live-Tabelle ist varchar(10)". Diese Prämisse ist seit dem ALTER falsch. Der Wert wurde
**nicht** angehoben — das würde die Cooldown-Keys auf dem Geld-Pfad ändern; nur der Kommentar ist
korrigiert.

**RSI-Execute — bereits dokumentiert.** Der beauftragte CHANGELOG-Eintrag existiert schon (Eintrag
`[2026-07-12]`, mit 88.426.142 Zellen / 3.831 Tabellen / 9,6 h / Idempotenz-Nachlauf 0). Kein zweiter
Eintrag, kein Pseudo-Output.

**Befund A — `restart_fleet.ps1` bricht Restarts nach erfolgreichem Pull ab.** Zweimal belegt
(`logs/fleet_restart_20260726_232251.log`, `_20260801_192843.log`): `ERROR - Pull failed: From
https://github.com/ziagl888/Kythera`, danach „Fleet untouched" und Exit 1 — obwohl der Pull
durchlief (HEAD 0e432d5 → e3181d5, im Folgelauf zwei Minuten später als „nothing to pull" bestätigt).
Ursache: git schreibt Fortschritt auf **stderr**; PowerShell 5.1 macht daraus ErrorRecords, sobald der
Strom in die Pipeline gemergt wird — was passiert, wenn der Operator das Skript mit `2>&1` aufruft.
Mit `$ErrorActionPreference = 'Stop'` terminiert die erste Fortschrittszeile, und die Exception-
Message ist genau der erste stderr-Text. Reproduziert in einem Scratch-Repo (alt: Abbruch mit
identischer Signatur / neu: sauberer Durchlauf) und end-to-end am echten Skript per `-DryRun`.
Fix: stderr explizit mergen, Fehler für die Dauer des Aufrufs demoten, **Exit-Code als einziges
Urteil**, stderr als INFO ins Log. Echte git-Fehler werfen weiterhin — jetzt sogar mit git's Text
statt nur einem Exit-Code.

**Befund B — der lokale Secret-Guard aus harter Regel 3 ist auf SRV02 nicht scharf.** Weder
`pre-commit` noch `gitleaks` liegen auf dem PATH (`Get-Command` → beides „NOT ON PATH"; ebenso `ruff`
und `mypy`, die nur als Python-Module verfügbar sind). Damit läuft auf diesem Host **kein**
Secret-Scan und **kein** `guard.py verify` beim Commit — beides existiert nur noch als CI-Regex bzw.
gar nicht. Für diese Session wurden die Äquivalente von Hand gefahren (siehe PR-Text). Das ist ein
Host-Setup-Punkt, kein Code-Punkt: `--no-verify` wurde nicht benutzt, es gibt schlicht nichts zu
umgehen.

---

## 7 · Offene Entscheide für Michi

**Entscheid 1 — Dashboard-Exposure (P0.8/Z2).** Der Port hängt heute offen im Netz, wird gescannt,
und `stop_all` ist unauthentifiziert erreichbar. Drei Wege:

| Option | Wirkung | Kosten |
|---|---|---|
| (a) `DASHBOARD_BIND_HOST=127.0.0.1` in `.env`, wirksam beim nächsten Restart | Surface sofort zu | Fernzugriff weg, bis Tunnel steht |
| (b) Erst `cloudflared` + Cloudflare Access, dann (a) | Surface zu **und** Fernzugriff bleibt | braucht Domain + Installation (~0,5–1 Tag) |
| (c) Windows-Firewall-Regel auf Port 5000 | Surface sofort zu, ohne Code | Host-Änderung, außerhalb des Freigabe-Rahmens dieser Session |

Empfehlung: (a) sofort, (b) danach in Ruhe. Der Knopf ist gebaut und default-off; **kein** Flip aus
dieser Session.

**Entscheid 2 — Rollout des P0.7-Fixes.** Wirksam erst beim nächsten Fleet-Restart. Erwartete
Wirkung: SR verliert ~1,1 % seiner Signale ganz und korrigiert bei ~10 % die Ziel-Leiter; Main
Channel 2,1 % / 6,4 %. Die entfallenden Signale sind genau die, deren TP1 auf der Verlustseite lag.
Nebenwirkung, die dazugehört: die Trefferquote von „Support Resistance" wird nach dem Rollout
**sinken** — die 96,5 % status≥1 der falschseitigen Trades waren Phantom-Treffer. Das Orchestrator-
Gating sieht danach einen echten, niedrigeren Wert.

**Entscheid 3 — `COOLDOWN_MODULE_MAX_LEN` von 10 auf 50 anheben?** DB-seitig seit dem ALTER frei.
Ändert Cooldown-Keys auf dem Geld-Pfad (heute fällt `25_smc_ml_sniper` bei langen Tags auf einen
statischen Tag zurück). Nicht angefasst.

**Nicht-Entscheid:** Job 8 braucht keine Freigabe, sondern eine saubere Messung ohne Session.
Job 10 braucht einen kleinen Folge-Task (QM/SRA1-Adapter), keine VPS-Sitzung.
