# Per-Bot-Performance: Komplett-Umbau

## Was wurde geändert

Vier zusammenhängende Probleme aus deinem letzten Screenshot sind gefixt:

### 1. MIS1-Konsolidierung
`MIS1-8h_pump` + `MIS1-8h_dump` → **`MIS1-8h`**
`MIS1-24h_pump` + `MIS1-24h_dump` → **`MIS1-24h`**
`MIS1-72h_pump` + `MIS1-72h_dump` → **`MIS1-72h`**
`MIS1-168h_pump` + `MIS1-168h_dump` → **`MIS1-168h`**

Die `_pump`/`_dump`-Suffixe sind eigentlich nur Synonyme für LONG/SHORT.
Die Information `direction` ist sowieso separat gespeichert — also bringt
das Suffix nichts außer künstlich aufgeblasene Bot-Listen.

### 2. MSI1-Typo-Fix
`MSI1-*` → `MIS1-*` (historische falsche Einträge werden bei der Anzeige
umgemapped; die DB bleibt unverändert).

### 3. "ALL"-Spalte repariert
Der 0%-Bug bei EPD1 & Co ist weg. Root cause: Die Anzeige zählte auch
**offene Trades** (Shadow-Inserts ohne Close) als Verlust. Now
**nur geschlossene Trades** in WR-Berechnungen einbezogen.

### 4. Zeitfenster-Logik migrated
Filter jetzt nach **`created_at`** (Eröffnungszeit), nicht mehr `closed_at`.
Semantik: "1h" = "Trades die in der letzten Stunde eröffnet wurden".
Statistisch sauberer — ein 168h-MIS1-Signal beeinflusst nicht mehr die
1h-Spalte weil es heute zufällig schließt.

### 5. Neue Detail-line für 4h
Unter jedem Bot erscheint eine 3-linen-Detail-Ansicht für die letzten 4h:

```
MIS1-8h      │  33%↓ │  67% │  69% │  63% │  65%   (n=3000, +1.04%)
  4h: 10 opened → 6 closed, 4 still open
    TP1+:4 TP2+:2 TP3+:1 TP4:0 | SL:2
    LONG: 3/4 win | SHORT: 1/2 win
```

- `4h: X opened → Y closed, Z still open` — Summe X = Y + Z
- `TP1+:X` = hat mindestens TP1 erreicht (status ≥ 1)
- `TP2+:X` = hat mindestens TP2 erreicht (status ≥ 2)
- `TP3+:X` = hat mindestens TP3 erreicht
- `TP4:X` = Vollhit (status = 4)
- `SL:X` = Verlust (status = 0)
- Summe der TP1+ und SL = closed (weil status ∈ {0,1,2,3,4} deckt alles ab)
- LONG/SHORT-Split zeigt asymmetrische Bot-Performance

## Wichtige technische Details

### Neue Datenquellen
Die Funktion zieht jetzt aus **vier** Tabellen:
- `closed_trades_master` — klassisch, geschlossen
- `active_trades_master` — klassisch, offen
- `closed_ai_signals` — AI, geschlossen
- `ai_signals` (JOIN mit `ml_predictions_master`) — AI, offen

Für `ai_signals` gibt es keine direkte `created_at`-Spalte; wir joinen
mit `ml_predictions_master.time`. Falls der JOIN fehlschlägt, gibt's
einen Fallback der einfach `NOW()` als created_at nimmt (= Trade wird
der aktuellen Stunde zugeordnet — akzeptabler Graceful Degradation).

### Sortierung
Bots are now nach **geschlossenen Trades** sortiert (`n_closed_total`),
nicht nach Total. So kommen Bots mit tatsächlicher Historie nach oben.

### n=X in der Haupt-line
Zeigt jetzt nur noch die Zahl der **geschlossenen** Trades (nicht opened).
Das ist konsistent mit der WR-Berechnung.

### Kelly-Berechnung
Basiert unverändert auf allen geschlossenen Trades. Hat jetzt konsistent
dieselbe Datenbasis wie die All-Spalte.

### linen-Anzahl im Post
Pro Bot jetzt 4 linen (statt 1):
- Haupt-line (Win-Rates)
- `4h: X opened → Y closed`
- `TP1+:...`
- `LONG: ... | SHORT: ...`
- Leerzeile

Bei 46 Bots wird das länger — der Split-Mechanismus von der letzten Version
greift trotzdem: Tabelle + Kelly-Block werden weiterhin auf mehrere
Messages verteilt falls nötig.

## Getestet

Mit 180.000 Mock-Trades über 7 Strategien, inklusive:
- EPD1 mit 70k Trades und 60% WR → Anzeige zeigt 60% (vorher 0%)
- ATS1 mit 100k Trades und 58% WR → Anzeige zeigt 58% (vorher 1%)
- MIS1-8h_pump + MIS1-8h_dump → wird als eine line "MIS1-8h" angezeigt
- MSI1-24h_pump → landet bei MIS1-24h
- Offene Trades erscheinen als "still open" in der Detail-line
- Target-Staffelung: TP1+ ≥ TP2+ ≥ TP3+ ≥ TP4, Summe = closed

## Deploy

Eine Datei ersetzen:
```
C:\_BOTS\crypto_trading_bot_v2\23_market_tracker.py
```

Watchdog neu starten. Ab nächstem XX:00:30 sollte der Post:
- Korrekte WR-Zahlen zeigen (keine 0%-Geister-Werte mehr)
- MIS1 in den konsolidierten Horizont-Versionen zeigen
- Detail-line mit Target-Staffelung unter jedem Bot haben

## Was bleibt gleich

- Kelly-Block (Half-Kelly, Safe Margin, Pure Margin)
- Message-Split bei 46+ Strategien (von letzter Runde)
- HTML-Format (nur `<pre>`, `<b>`, `<i>` ohne style-Attribute)
- Tabellen-Layout (Spalten-Alignment)

## Falls irgendwas doch kaputt ist

Die Änderungen sind primär additiv:
- Bei leerer `active_trades_master`/`ai_signals` Tabelle: kein Crash, einfach `0 still open`
- Bei fehlendem ml_predictions_master-JOIN: Fallback auf NOW()
- Bei Bot ohne 4h-Aktivität: Detail-line wird weggelassen

Falls du im Log Fehler siehst: schick mir den Output, dann flicke ich sie.
