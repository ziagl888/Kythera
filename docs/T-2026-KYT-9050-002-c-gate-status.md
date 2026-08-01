# C-Gate: Ist-Stand, Mengengerüst und offene Operator-Entscheidungen

**Stand:** 2026-08-01 (Messung 19:54–20:13 UTC auf SRV02, Live-DB `cryptodata`) · **Task:** T-2026-KYT-9050-002 · **Design:** `docs/TIMESCALE_R1_MIGRATION.md` · **Inventar:** `docs/CANDLE_CALL_SITES.md` · **Entscheid-Record:** D-2026-CLD-109

Alle Zahlen unten sind in dieser Session selbst gemessen (read-only, keine Schreib-Query).
Nicht selbst Verifiziertes ist als **[nicht verifiziert]** markiert.
Zeiten in UTC; die DB-Session antwortet in `Europe/Bucharest` (+03), Umrechnung ist erfolgt.

---

## 1. Die Kernaussage: die C-Gate ist nicht mehr dormant, sie ist live

Das Design-Doc und der KB-Task-Brief beschreiben die Phasen 3–5 als offen und die
Phase-2/4-Slices als „gebaut, aber dormant". **Das ist überholt.** Gemessen:

| Flag (`.env` der Live-Fleet) | Wert |
|---|---|
| `KYTHERA_CANDLES_DUAL_WRITE` | `1` |
| `KYTHERA_CANDLES_SOURCE` | `hyper` |
| `KYTHERA_CANDLES_WRITE_PRIMARY` | `hyper` |

Die Fleet läuft seit **2026-08-01 16:30–16:34 UTC** neu (`logs/fleet_restart_20260801_193042.log`,
HEAD `e3181d5`) und liest damit die Hypertables.

**Wann der Umschwung wirklich passierte — belegt, nicht aus dem Ticket:**
Die per-Coin-Tabellen enden alle exakt bei `open_time = 2026-07-16 16:00 UTC` (1h) bzw.
`16:20 UTC` (5m). Der Watchdog-Neustart davor ist `logs/watchdog_debug_20260716_192326.log`
(2026-07-16 16:23 UTC). `core/candles.py` überspringt bei `write_primary == "hyper"` den
per-Coin-Write vollständig. Kette geschlossen: **`WRITE_PRIMARY=hyper` ist seit
2026-07-16 16:23 UTC wirksam — seit 16 Tagen.**

Was heute um 10:13 UTC an der `.env` geändert wurde, lässt sich aus der Datei-mtime allein
nicht rekonstruieren — **[nicht verifiziert]**, welches der drei Flags das war.

### Der UTC-Flip ist NICHT mit aktiviert worden

Die im Brief befürchtete Kopplung ist nicht eingetreten:
`git merge-base --is-ancestor 3ba3bbd e3181d5` → **nein**. Der R3-UTC-Flip aus
T-2026-KYT-9050-005 liegt auf `main`, aber **nicht** im laufenden Fleet-Stand. Die beiden
Änderungen sind bereits entkoppelt; der nächste Restart aktiviert den UTC-Flip allein.

Strukturell war der Kerzen-Pfad ohnehin nie exponiert: `open_time` ist auf **9.804 von 9.806**
per-Coin-Tabellen `timestamp with time zone`. Die einzigen zwei naiven Spalten liegen in
`ai_signals` und `closed_ai_signals` — beide nicht Teil dieser Migration. Ein
Session-TZ-abhängiger Cast im Backfill (`INSERT … SELECT open_time`) war damit nie möglich.

---

## 2. Mengengerüst (gemessen)

PostgreSQL 17.6 · TimescaleDB 2.26.3 · Datadir `C:/PGDATA`

### Speicher

| Objekt | Größe |
|---|---|
| Legacy per-Coin gesamt (9.683 Tabellen, inkl. Indexe) | **64 GB** |
| davon Kerzen-Tabellen (5.522) | 9.969 MB |
| davon Indikator-Tabellen (4.161) | **54 GB** |
| Hypertable `candles` (45,0 Mio. Zeilen) | 9.954 MB (Heap 4.559 MB / Index 5.394 MB) |
| Hypertable `indicators` (18,6 Mio. Zeilen) | 20 GB (Heap 18 GB / Index 1.889 MB) |
| **Datenbank gesamt** | **98 GB** |
| Laufwerk C: | 263 GB gesamt, **78 GB frei** |

**Die 25-GB-Annahme des Design-Docs ist um Faktor ~2,5 zu niedrig** — der Legacy-Bestand ist
64 GB, und der Löwenanteil sind die Indikator-Tabellen (54 GB), nicht die Kerzen. Der
Design-Doc-Satz „25 GB → 4–6 GB" beschreibt eine Ausgangsgröße, die es nie gab.

Ebenso überholt: die Risiko-Zeile „C: hat ~160 GB frei". Real sind es **78 GB**. Der
Doppelbestand (64 GB legacy + 30 GB hyper) wird heute gleichzeitig vorgehalten.

### Kompression — der eigentliche offene Hebel

| | |
|---|---|
| Chunks `candles` / `indicators` | 128 / 128 |
| davon komprimiert | **0 / 0** |
| `compression_enabled` | **false** auf beiden |
| Compression-/Retention-Policies | **keine** (`timescaledb_information.jobs` leer für beide) |

Das ist konsistent mit D-2026-CLD-109 (Compression bewusst auf Phase 5 vertagt) — aber es
heißt, dass der gesamte erwartete Speichergewinn bis heute **unrealisiert** ist.

**Gemessener Anker aus derselben Datenbank** statt einer Schätzung aus dem Design-Doc:
`oi_5m` ist komprimiert und liefert **652 MB → 78 MB = Faktor 8,35**. `ticker_10s` ist
ebenfalls komprimiert.

Vorsicht bei der Übertragung: `oi_5m` ist eine schmale, stark repetitive Tabelle.
`indicators` hat **108 `double precision`-Spalten** mit weitgehend nicht-repetitiven Floats
und wird deutlich schlechter komprimieren. Seriöse Spanne, **als Schätzung gekennzeichnet**:
30 GB → **4–10 GB**. Wer die echte Zahl will, misst sie an genau einem Chunk (siehe §4-A).

### Datenqualität und R1-Vertrag (gemessen)

- **527 Symbole** wurden in den letzten 90 Minuten geschrieben.
- Kontinuität über die 07-16-Grenze: `BTCUSDT_1h` in Kerzen **und** Indikatoren
  durchgehend 24 Zeilen/Tag vom 07-12 bis 07-21 — **keine Lücke** durch den Umschwung.
- `is_closed = false` trifft exakt **527 Zeilen je Timeframe** = ein forming Candle pro
  Symbol. Der R1-Speichervertrag greift.
- Indikator-Frische ist je Timeframe **exakt closed-only-korrekt** (Messung 19:54 UTC):
  1h → 18:00, 2h → 16:00, 4h → 12:00, 1d → 07-31, 1w → 07-20. Kein Look-ahead, kein Nachlauf.
- Backfill-Deckung: 9.669 von 9.683 (symbol, tf, kind) sind im Progress-File. Die 14 Fehlenden
  sind **ausnahmslos `GRVTUSDT`** (Neu-Listing nach dem Backfill vom 07-14) — dessen
  Legacy-Tabellen sind **leer** (0 Zeilen), es ist also keine Historie verloren gegangen.

---

## 3. Bereitschaft der beiden dormanten Tools (selbst gefahren)

Getestet unter beiden Interpretern: Session-Python 3.14.6 / pandas 3.0.3 **und**
Fleet-Python 3.13.12 / pandas 2.3.2.

| Lauf | Ergebnis |
|---|---|
| `candles_parity.py --self-check` (3.14 und 3.13) | **OK**, Exit 0 |
| `candles_parity.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --tf 1h --days 3` gegen Live-DB | **läuft**, Exit 1, 9 Drift-Findings |
| `candles_backfill.py` Dry-Run (3.14 und 3.13) | **OK**, Exit 0, Plan über 9.683 Tabellen |

Kein Code-Bruch. `core.time.epoch_seconds` berührt keinen der beiden Pfade
(`candles_backfill` importiert nur `utc_now`).

### Aber: das Paritäts-Tool ist in seiner Bestimmung nicht mehr einsetzbar

Der Live-Lauf meldet für **jedes** Symbol `rows old=0` — die Legacy-Seite ist in jedem
aktuellen Fenster leer, weil sie seit 07-16 nicht mehr geschrieben wird. Die 9 Drift-Zeilen
sind Artefakte der Phase, kein Datenproblem.

**Konsequenz: das Phase-3-Gate („0 Drift an 3 aufeinanderfolgenden Tagen") lässt sich nicht
nachträglich erfüllen.** Die Voraussetzung des Tools (Fleet liest legacy, schreibt beides)
ist seit 16 Tagen vorbei. Als nächtlicher Cron würde es jede Nacht Exit 1 liefern.
Ein Cron-Eintrag oder Parity-Log war nicht auffindbar — **[nicht verifiziert]**, ob er je
eingerichtet wurde.

### Operativer Stolperstein im Backfill

`PROGRESS_FILE` wird relativ zum **Checkout** aufgelöst
(`<repo>/control/candles_backfill_progress.json`). Aus diesem Worktree gefahren meldet der
Backfill „0 already done" und würde alle 9.683 Tabellen erneut anfassen. Durch
`ON CONFLICT DO NOTHING` ist das idempotent und ungefährlich, aber es liest den kompletten
Legacy-Bestand sinnlos erneut. **Der Backfill gehört ausschließlich aus
`C:\Users\Michael\Documents\Kythera` gefahren**, wo die 9.669 Einträge liegen.

---

## 4. Die offenen Operator-Entscheidungen — mit Zahlen

Zwei der drei im Task-Brief genannten Punkte sind **keine offenen Fragen mehr**:

- **Retention:** entschieden (D-2026-CLD-109 #1, unbegrenzt, keine Policy). Der Ist-Stand
  bestätigt es: keine Retention-Policy auf beiden Hypertables. **Kein Handlungsbedarf.**
- **Startzeitpunkt:** gegenstandslos — gestartet am 2026-07-16 16:23 UTC.

Offen — und mit Zahlen unterlegt — sind diese:

### A. Compression aktivieren

- **Einsatz:** `ALTER TABLE … SET (timescaledb.compress, segmentby='symbol, tf',
  orderby='open_time DESC')` + `add_compression_policy(…, INTERVAL '14 days')`, wie im
  Design-Doc §1 vorgesehen.
- **Erwarteter Gewinn:** 30 GB → 4–10 GB (**Schätzung**; gemessener Anker `oi_5m` = 8,35×,
  aber `indicators` ist mit 108 Float-Spalten deutlich ungünstiger).
- **Billigste Falsifikation vor der Policy:** genau **einen** Chunk komprimieren
  (`compress_chunk`) und `chunks_detailed_size` vorher/nachher vergleichen. Das ist
  reversibel (`decompress_chunk`) und liefert die echte Zahl statt meiner Spanne.
  Der größte `candles`-Chunk ist `_hyper_38_643_chunk` (492 MB, 07-23 bis 07-30).
  **Das ist eine Schreib-Operation → Operator-Entscheidung, in dieser Session nicht gefahren.**
- **Nebenwirkung:** Upserts in komprimierte Chunks sind langsamer. Bei 14 Tagen Karenz
  trifft das nur Backfill-artige Nachträge, nicht den Live-Stream.

### B. Legacy-Tabellen droppen (Phase 5)

- **Einsatz:** 64 GB, das sind ~65 % der 98-GB-Datenbank. Der größte einzelne Hebel,
  größer als die Kompression.
- **Reife:** Der Design-Doc verlangt „erst 7 Tage nach dem Cutover". Es sind **16 Tage**.
- **Vorbedingung 1 — pg_dump + Restore-Test.** Design-Doc-Gate, nicht verhandelbar.
- **Vorbedingung 2 — sonst wachsen sie nach.** `6_housekeeping.py:67` feuert bei jedem
  `coins.json`-Update ein `CREATE TABLE IF NOT EXISTS "{symbol}_{tf}"` über alle Symbole ×
  Timeframes. Genau daher stammen die leeren `GRVTUSDT`-Tabellen. **Ein Drop ohne Entfernen
  dieser Schleife stellt binnen eines Housekeeping-Zyklus ~4.200 leere Tabellen wieder her.**
- **Vorbedingung 3 — die zwei Leser aus §5 müssen vorher entschieden sein.**

### C. Was mit den zwei verbliebenen Legacy-Lesern geschieht (§5)

### D. Reihenfolge Dual-Write vs. UTC-Flip

Beantwortet und gegenstandslos: die Kerzen-Migration ist vollständig aktiv, der UTC-Flip
ist es nicht (§1). Am Code begründet ist auch, dass es nie eine Kopplung gab — `open_time`
ist praktisch überall `timestamptz`, der Backfill-Cast war nie session-TZ-abhängig.

---

## 5. Zwei Bots lesen seit 16 Tagen eingefrorene Tabellen

Das ist der einzige gefundene echte Defekt.

Genau **zwei** Live-Fleet-Dateien lesen noch roh aus per-Coin-Tabellen, an `core.candles`
vorbei:

| Datei | Zeile | Liest | Kanal |
|---|---|---|---|
| `16_smc_forex_metals_bot.py` | 87 | `FROM "{symbol}_{tf}"` (METALS-Gruppe, `source="database"`) | `CH_SMC_METALS` |
| `21_btc_smc_strategy.py` | 136 | `FROM "{SYMBOL}_{TIMEFRAME}"` (BTCUSDT 1h) | `CH_BTC_SMC` |

Beide sind im Watchdog-Roster des aktuellen Fleet-Laufs und wurden am 2026-08-01 gestartet.
Beide lesen damit Kerzen, die am **2026-07-16 16:00 UTC** enden — 16 Tage alt.

**Das war kein Versehen, sondern eine bewusste Zurückstellung.** `docs/CANDLE_CALL_SITES.md`
führt beide in Block C mit dem Ziel „F — Caller-Drop entfernen" und listet sie in §3 unter
„Index-gekoppelt — Flip nur zusammen mit Offset-Rework". Die Zurückstellung setzte aber
stillschweigend voraus, dass die Legacy-Tabellen maßgeblich bleiben. Mit dem
`WRITE_PRIMARY=hyper`-Flip am 07-16 ist diese Prämisse entfallen.

### Blast-Radius: real, aber bisher folgenlos — verifiziert

- `CH_SMC_METALS`: **9 Posts im gesamten erhaltenen Outbox-Fenster** (seit 2026-04-17),
  der letzte am **2026-07-16 09:35 UTC** — also noch vor dem Freeze. Seither nichts.
- `CH_BTC_SMC`: **null Posts** im gesamten Fenster.
- `CH_SMC_FOREX` (dieselbe Datei, aber `source="yfinance"`, vom Freeze nicht betroffen)
  postet normal weiter: 340 Posts, zuletzt 2026-08-01 00:50 UTC.

**Es ist also kein Signal aus veralteten Daten entstanden.** Das ist Glück, kein Design:
beide Bots emittieren einen **Cornix-parsebaren Block** (`16_…:344-352`), d. h. ein Signal
aus 16 Tage alten Preisen wäre ein echter Geld-Vorfall gewesen.

### Warum hier kein Fix committet wurde

Die naheliegende Reparatur — `fetch_db_data` auf `core.candles.read_candles` umverdrahten —
ist technisch klein und wäre byte-parität-fähig (`include_forming=False` **plus** Entfernen
des Caller-Drops `df.iloc[:-1]` in Zeile 392, sonst geht eine geschlossene Kerze zusätzlich
verloren; genau deshalb steht der Offset-Rework im Inventar).

Sie ist aber **keine reine Code-Korrektur**: beide Bots sind faktisch seit 16 Tagen
stillgelegt. Sie wieder an Live-Daten zu hängen heißt, einen Cornix-postenden Bot zu
**entparken** — und das ist nach `docs/OPUS-HANDOFF.md` §6 ausdrücklich eine
Operator-Entscheidung, kein Session-Entscheid. Die Alternative ist ebenso legitim: beide
bewusst über `control/parked/` parken und die Zeilen im Inventar schließen.

**→ Entscheidung Michi:** reparieren (Signale kehren beim nächsten Restart zurück) **oder**
bewusst parken. Vorher darf keine der beiden Zeilen als „erledigt" gelten und die
Legacy-Tabellen dürfen nicht gedroppt werden.

---

## 6. Rollback ist nicht mehr trivial — die Design-Zusage ist gebrochen

`docs/TIMESCALE_R1_MIGRATION.md` §3 sagt: „Rollback ist in jeder Phase trivial: Bis Phase 4
liest die Fleet die alten Tabellen; der Cutover selbst ist ein Env-Flag + Restart zurück."

Das gilt nicht mehr, und `core/candles.py:352-355` beschreibt genau warum
(Rollback-Asymmetrie des Write-Primary). Der dort beschriebene Fall ist eingetreten:

> **`KYTHERA_CANDLES_SOURCE=legacy` ist heute eine geladene Waffe.** Ein Zurückflippen
> „zum Rollback" setzt die gesamte Fleet auf Kerzen, die am 2026-07-16 enden — still,
> ohne Fehler, mit vollem Geld-Effekt. Ein Rollback der Leseseite braucht **zuerst** einen
> Backfill der 16-Tage-Lücke in die per-Coin-Tabellen.

Die Gegenrichtung ist unkritisch: der Hyper-Store wird durchgehend geschrieben und braucht
nie einen Nachtrag.

**Empfehlung:** diesen Zustand nicht offen lassen. Entweder die Lücke rückwärts füllen
(macht Rollback wieder billig, kostet aber erneut Platz auf 64 GB, die man eigentlich
loswerden will) — **oder** vorwärts entscheiden, die Legacy-Tabellen droppen (§4-B) und den
`legacy`-Zweig aus `core/candles.py` samt Flag entfernen, damit niemand mehr zurückflippen
*kann*. Der zweite Weg ist der ehrlichere: er macht sichtbar, dass es kein Zurück gibt,
statt eines Rollback-Pfads, der nur auf dem Papier existiert.

---

## 7. Paritäts-Plan: was gemessen wird, was nicht, und was ein Stopp ist

Vorab definiert, damit der Drop-Entscheid später an Zahlen hängt und nicht an Auslegung.

### Was `tools/candles_parity.py` heute vergleicht

Je (symbol, tf) über ein Fenster von N Tagen, **nur geschlossene Kerzen**
(Cutoff `core.candles.period_start`, dieselbe Uhr wie die Leser-API):

1. Zeilenzahl im Fenster
2. `max(open_time)`
3. SHA-256-Prüfsumme über die OHLCV-Tupel, ASC, Floats auf 12 signifikante Stellen
   kanonisiert (damit `REAL`-vs-`double`-Rauschen nicht als Drift zählt)

### Was es ausdrücklich **nicht** vergleicht

- **Indikatoren** — gar nicht. Das sind 54 GB legacy bzw. 20 GB hyper und 108 Spalten,
  also der größere und heiklere Teil des Bestands. Er ist ungeprüft.
- Die `is_closed`-Spalte selbst (bewusst: der Cutoff ist uhr-, nicht flag-basiert).
- Alles außerhalb des Fensters — insbesondere die **Alt-Historie**, die der Backfill kopiert hat.
- Spaltentypen, Zeilen jenseits von OHLCV, Symbol-/TF-Zuordnung.

### Warum der ursprüngliche Plan nicht mehr greift

Legacy-vs-Hyper ist seit dem Write-Primary-Flip strukturell sinnlos (§3). Für die noch
offene Entscheidung — den **Drop** — ist ohnehin eine andere Frage relevant: nicht „stimmen
beide Seiten heute überein", sondern **„ist im Hyper-Store alles enthalten, was in den
Legacy-Tabellen steht, bevor diese verschwinden"**.

### Vorgeschlagenes Ersatz-Gate vor dem Drop

Gegen den **Bestand**, nicht gegen den Live-Strom. Auf einer Stichprobe von ≥50 Symbolen
über alle Timeframes, plus vollständig für die 8 liquidesten:

| # | Prüfung | Stopp-Kriterium |
|---|---|---|
| G1 | Für jede (symbol, tf): `count(*)` und `min/max(open_time)` legacy vs. hyper über die **gesamte** Legacy-Historie | **jede** fehlende Zeile im Hyper-Store, die legacy hat → Stopp |
| G2 | OHLCV-Prüfsumme (bestehende `checksum_rows`-Logik) über die volle Legacy-Spanne, nicht nur N Tage | ein Prüfsummen-Mismatch → Stopp |
| G3 | Dasselbe für **Indikatoren** — heute nicht abgedeckt, muss ergänzt werden | jede Abweichung → Stopp |
| G4 | `pg_dump` der Legacy-Tabellen + **Restore-Test** in eine Wegwerf-DB | Restore schlägt fehl oder Zeilenzahlen weichen ab → Stopp |
| G5 | §5 entschieden (Bots 16/21 repariert **oder** geparkt) | offen → Stopp |
| G6 | `6_housekeeping.py:67`-CREATE-Schleife entfernt | offen → Stopp (Tabellen wachsen sonst nach) |

Toleranz bewusst bei **null**: die Legacy-Seite ist eingefroren und unveränderlich, es gibt
also keine Race-Bedingung, die eine Toleranz rechtfertigen würde. Jede Abweichung ist ein
echter Fund. Das unterscheidet dieses Gate vom ursprünglichen Live-Paritäts-Cron, wo eine
Toleranz nötig gewesen wäre.

G1–G3 brauchen eine Erweiterung von `candles_parity.py` (Voll-Spanne statt Fenster,
Indikator-Modus). Das ist die nächste sinnvolle Bau-Arbeit an diesem Task — sie ist erst
nach Michis Entscheid zu §4-B/§5 sinnvoll und wurde daher hier nicht vorweggenommen.

---

## 8. Was diese Session bewusst NICHT getan hat

- Kein Flag gesetzt, kein Restart, kein Cutover, keine DDL, keine Schreib-Query.
- Keine Kompression aktiviert und **auch keinen Probe-Chunk komprimiert** — das wäre ein
  Write gewesen; die Zahl in §4-A bleibt deshalb ehrlich eine Schätzung.
- Bots 16/21 nicht repariert (§5, Begründung dort).
- `tools/candles_parity.py` nicht auf das neue Gate erweitert (§7, wartet auf Entscheid).
