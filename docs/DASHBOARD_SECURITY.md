# Dashboard-Absicherung (P0.8 / Z2-B4) — Ist-Zustand, Code-Fix, offener Entscheid

**Stand:** 2026-08-02 · **Task:** T-2026-KYT-9050-056 · **Ledger:** `AUDIT_TODO.md` P0.8 + P1.38
(CSRF-Teil), Task-Audit B4/Z2 · **Code:** `core/dashboard_security.py`, `dashboard.py`

Dieses Dokument hält fest, was am Dashboard **gemessen** exponiert war, was der zugehörige PR
im Code ändert, und welcher Teil der Absicherung **nicht** gebaut werden konnte, weil er
Exposure voraussetzt — Dashboard-Exposure steht auf Michis Eskalationsliste
(`CLAUDE.md` §Eskalation, OPUS-HANDOFF §6).

---

## 1. Ist-Zustand — gemessen, nicht aus den Akten übernommen

Alle Werte am 2026-08-01/02 auf srv02 selbst erhoben (read-only, kein Eingriff).

> ### ⚠️ Korrektur 2026-08-02 — die Firewall-Zeilen dieser Tabelle waren falsch
>
> Nachgemessen beim Merge dieses PRs, read-only:
>
> | Was | Ursprünglich notiert | Tatsächlich |
> |---|---|---|
> | Effektive Inbound-Default-Aktion | **Block** (alle drei Profile) | **`NotConfigured`** (alle drei Profile) |
> | Inbound-Allow-Regel, die TCP 5000 trifft | **keine** | **zwei** — `SMC Service` + `SNAC Service`, Enabled/Inbound/**Allow**/Public, `LocalPort: Any`, `RemoteIP: Any` (Symantec) |
> | Port 5000 aus dem Internet erreichbar | **nein** | **ja** — `logs/dashboard.log` zählt seit 04.07. **557** erfolgreiche `GET / → 200` an fremde IPs |
>
> Die Regel-Abfrage der Ursprungssitzung lief in „Access is denied" (nicht elevated); daraus
> wurde „keine Allow-Regel" geschlossen. **Eine gescheiterte Messung ist kein Negativbefund.**
> Die Sitzung hatte ihre Prüfmethode für die Erreichbarkeit sogar korrekt als untauglich
> markiert — eine Verbindung von der Box auf ihre eigene öffentliche IP behandelt Windows als
> Loopback — und dann trotzdem der Konfiguration geglaubt statt der Empirie im eigenen Log.
>
> **Folge für dieses Dokument:** §"Was daraus folgt" unten argumentiert an mehreren Stellen mit
> „die Firewall blockt" — diese Begründung entfällt. Die daraus abgeleiteten Maßnahmen bleiben
> richtig; die Härtung dieses PRs steht nicht mehr hinter einer Firewall, sondern trägt allein.
>
> **Entscheid dazu (2026-08-02, Operator): die beiden Symantec-Regeln bleiben aktiv —
> WONTFIX, Risiko akzeptiert (`T-2026-KYT-9050-070`).** Das ist kein offener Punkt mehr und
> soll nicht erneut als Finding aufgemacht werden. Begrenzend wirken zwei Dinge, die
> unabhängig von der Firewall greifen: Postgres (5432) ist zwar erreichbar, `pg_hba.conf`
> kennt aber nur `127.0.0.1/32`, `::1/128` und den lokalen Socket → Fremde werden abgewiesen,
> kein Datenzugriff; und das Dashboard bindet ab dem nächsten Start per Default auf Loopback,
> womit der unauthentifizierte `POST /api/system/stop_all` aus dem Netz verschwindet.
> Exponiert bleiben 135 (RPC), 445 (SMB), 3389 (RDP) und 5985 (WinRM).
>
> Wiedervorlage nur bei: (a) einem Fremd-Treffer auf einem Control-Endpoint im
> Dashboard-Log, (b) einer `pg_hba`-Zeile für eine externe IP, (c) Symantec-Deinstallation.
> Die Umkehrung bliebe jederzeit ein Kommando, elevated, sofort reversibel, **RDP unberührt**
> (RDP hat eigene Regeln): `Disable-NetFirewallRule -DisplayName "SNAC Service","SMC Service"`.

| Was | Messung | Wie gemessen |
|---|---|---|
| Legacy-Dashboard-Listener | **`0.0.0.0:5000`**, PID 100120, gestartet 2026-08-01 19:34 | `Get-NetTCPConnection -State Listen` |
| Z1-Dashboard-Shell | `127.0.0.1:8098`, PID 86852, läuft seit 2026-07-20 | dito |
| Analytics-API | eigener Prozess nicht gebunden; Default im Code `127.0.0.1:8099` | `tools/analytics_api.py:1647` |
| Firewall-Profile | Domain/Private/Public **alle enabled** | `Get-NetFirewallProfile` |
| ~~Effektive Inbound-Default-Aktion~~ | ~~**Block** (alle drei Profile, ActiveStore)~~ → **falsch, siehe Korrektur oben: `NotConfigured`** | `Get-NetFirewallProfile -PolicyStore ActiveStore` |
| ~~Inbound-Allow-Regel für TCP 5000~~ | ~~**keine**~~ → **falsch: `SMC Service` + `SNAC Service` erlauben jeden Port von jeder IP** | Scan lief unelevated ins „Access is denied" |
| Inbound-Allow-Regel für `python.exe`/`py.exe` | keine — aber gegenstandslos, die Symantec-Regeln filtern nicht auf Application | Scan aller aktiven Inbound-Allow-Regeln + Application-Filter |
| Öffentliche Adresse der Box | **45.134.39.167**, direkt geroutet (kein NAT) | `Get-NetIPAddress` |

**Route-Inventar des Legacy-Dashboards (11 Routen, Stand vor diesem PR):**

| Route | Methode | Wirkung | Auth vorher |
|---|---|---|---|
| `/` | GET | HTML-UI | keine |
| `/api/status` | GET | liest Fleet-/System-Status (psutil) | keine |
| `/api/logs/<script>` | GET | liest Bot-Logs (Strategie-Verhalten) | keine |
| `/api/logs/<script>/stream` | GET | Log-Live-Stream (SSE) | keine |
| `/api/events` | GET | Event-Stream (SSE) | keine |
| `/api/process/<script>/start` | POST | **schreibt** (unpark-Marker) | keine |
| `/api/process/<script>/stop` | POST | **schreibt** (park-Marker) | keine |
| `/api/process/<script>/restart` | POST | **schreibt** (restart-Marker) | keine |
| `/api/system/start_all` | POST | **schreibt** (alle Bots) | keine |
| `/api/system/restart_all` | POST | **schreibt** (alle Bots) | keine |
| `/api/system/stop_all` | POST | **schreibt** — parkt die ganze Fleet persistent | keine |

Die Park-Marker sind Dateien unter `control/parked/` und **überleben einen Reboot**: ein
einzelnes `POST /api/system/stop_all` legt die Fleet still, bis jemand die Marker entfernt.
Das Dashboard selbst führt nichts aus — der Watchdog ist der einzige Aktuator (`core/process_control.py`) —,
was am Ergebnis nichts ändert: er liest die Marker im nächsten Zyklus (≤10 s).

### Was daraus folgt (und was nicht)

* **Der Port WAR aus dem Internet erreichbar** (korrigiert 2026-08-02, siehe Kasten oben).
  Die Audit-Frage „Ist Port 5000 extern erreichbar?"
  (`audit_reports/10_dashboard_tools.md`, Frage 1 der DB-Phase) ist damit beantwortet:
  **ja** — belegt durch 557 beantwortete Fremd-Requests im eigenen Log, nicht durch eine
  Firewall-Konfiguration. Der Schutz hing an keiner Einstellung; es gab ihn nicht.
* **Nicht verifiziert:** eine Erreichbarkeitsprobe von einem **externen** Vantage-Point.
  Von der Box aus auf die eigene öffentliche IP zu verbinden beweist nichts — Windows
  behandelt das als Loopback und wendet die Inbound-Filter nicht wie bei echtem
  Fremdverkehr an. Die Aussage oben stützt sich auf das Regelwerk, nicht auf einen
  Verbindungstest von außen.
* **Der Schutz war ein Single Point of Failure.** Eine einzige Allow-Regel hebt ihn auf —
  auch die, die Windows beim ersten interaktiven Binden eines Listeners selbst zum Anlegen
  anbietet.
* **Zwei Angriffe funktionierten trotz Firewall** (das ist der eigentliche Befund):
  1. **CSRF per Simple-Request.** Eine beliebige Webseite im Browser auf der VPS kann
     `POST http://127.0.0.1:5000/api/system/stop_all` absetzen. Ein Form-POST bzw. ein
     `fetch(..., {mode:'no-cors'})` braucht keinen Preflight; die Antwort ist für den
     Angreifer opak, **die Nebenwirkung tritt trotzdem ein**. Firefox lief laut
     T-2026-CU-9050-166 zeitweise direkt auf der Box.
  2. **DNS-Rebinding.** Eine Angreifer-Domain, die auf `127.0.0.1` zeigt, erreicht dieselben
     Endpoints. Ein reiner Same-Origin-Vergleich hilft dagegen **nicht**, weil Angreifer-
     `Host` und Angreifer-`Origin` übereinstimmen — nur eine Host-Allowlist greift.

---

## 2. Was der Code-Fix ändert

`core/dashboard_security.py` (neu) + Verdrahtung in `dashboard.py`. Drei O(1)-Prüfungen pro
Request, in dieser Reihenfolge — keine DB, kein Prozess-Scan, kein Dateizugriff:

1. **Host-Allowlist** (alle Methoden). `Host` muss auf der Allowlist stehen
   (Default: `localhost`, `127.0.0.1`, `::1`, Bind-Adresse, Maschinenname; erweiterbar per
   `KYTHERA_DASHBOARD_ALLOWED_HOSTS`). → schließt DNS-Rebinding.
2. **Token** (alle Methoden, nur wenn konfiguriert). Konstantzeit-Vergleich gegen
   `KYTHERA_DASHBOARD_TOKEN`; Header `X-Dashboard-Token`, Cookie oder einmalig `?token=…`
   (setzt dann ein `HttpOnly`/`SameSite=Strict`-Cookie, damit die UI ohne Header weiterläuft).
3. **Origin** (nur zustandsändernde Methoden). Ein **vorhandener** `Origin` muss zum Host
   passen. Fehlender `Origin` bleibt erlaubt, damit curl/PowerShell-Aufrufe des Operators
   funktionieren; Browser senden ihn bei Cross-Origin-POSTs immer. → schließt CSRF.

Dazu:

* **Bind-Default `0.0.0.0` → `127.0.0.1`** (`KYTHERA_DASHBOARD_HOST` überschreibt).
* **Fail-closed-Startpolitik.** Der Prozess startet **nicht**, wenn (a) nicht-Loopback
  gebunden wird ohne Token, oder (b) ein Off-Box-Hostname in der Allowlist steht ohne Token.
  Fall (b) ist der Tunnel-Fall und der Grund, warum die Prüfung nicht nur an der Bind-Adresse
  hängt: `cloudflared` verbindet sich nach `127.0.0.1:5000`, die Bind-Adresse bleibt also
  harmlos, während das Dashboard weltweit erreichbar ist.
* **Control-Endpoints validieren den Skriptnamen** gegen `SCRIPT_MAP` (404 statt Marker-Datei
  für einen unbekannten Namen) — `audit_reports/10`, [LOW].

**Kosten pro Request:** einige String-Vergleiche und Dict-Lookups. Der teure Posten am
Dashboard bleibt unverändert `/api/status` (ein `psutil.process_iter`-Sweep pro Fleet-Eintrag,
alle 6 s pro Tab — P1.38, offen). Der Guard erzeugt **keine** zusätzliche Query und keinen
zusätzlichen Prozess-Scan.

**~~Verhaltens-Neutralität des Bind-Wechsels~~ — KORRIGIERT 2026-08-02, der Bind-Wechsel ist
NICHT neutral.** Die ursprüngliche Begründung („Off-Box-Zugriff ist heute nicht möglich, also
kann der Loopback-Bind keinen bestehenden Zugriffsweg kappen") steht auf der widerlegten
Firewall-Annahme (siehe Kasten oben). Off-Box-Zugriff **ist** möglich und wird genutzt — 537
beantwortete Fremd-Requests seit dem 04.07. **Der Loopback-Bind kappt also einen real
bestehenden Weg:** ab dem nächsten Dashboard-Start ist das Dashboard nur noch aus einer
RDP-Sitzung auf der Box erreichbar (und für Fremde gar nicht mehr — das ist der Zweck).
Fernzugriff braucht dann `KYTHERA_DASHBOARD_HOST` **plus** `KYTHERA_DASHBOARD_TOKEN` in `.env`
(ohne Token verweigert die Fail-closed-Politik den Start), oder den Tunnel aus Z2. Die Erfolgsprobe von
`tools/restart_fleet.ps1` (`Test-NetConnection -ComputerName localhost -Port 5000`) bleibt gültig:
Sie liefert schon heute gegen einen reinen IPv4-Listener `True`, obwohl `localhost` zuerst nach
`::1` auflöst — die Namensauflösung fällt auf IPv4 zurück (nachgemessen).

---

## 3. Was der PR NICHT tut

Kein Deploy, kein Dashboard- oder Fleet-Neustart, keine Firewall-Regel, kein Port, kein
Reverse-Proxy, kein `cloudflared`, keine `.env`-Änderung, keine Änderung an der laufenden
Bind-Adresse. Der laufende Dashboard-Prozess (PID 100120) ist unangetastet.

> **Wirksam wird der Fix erst beim nächsten Start des Dashboard-Prozesses.** Nach dem Merge
> passiert das **ohne Operator-Aktion**: der Watchdog startet das Dashboard bei einem Crash neu
> (`main_watchdog.check_dashboard`), und ein Reboot ohnehin. Wer den Zeitpunkt kontrollieren
> will, muss den Dashboard-Prozess bewusst neu starten (nicht die Fleet — der Watchdog zieht
> das Dashboard allein hoch).

---

## 4. ~~Offener Entscheid für Michi~~ — ENTSCHIEDEN 2026-08-02: **D1**

> **Operator-Entscheid (T-2026-KYT-9050-074): „Dashboard sehe ich ohnehin nur via RDP."**
>
> Damit gilt **D1** — Loopback-only, kein Token. **Es ist nichts zu tun und nichts zu
> konfigurieren:** weder `KYTHERA_DASHBOARD_HOST` noch `KYTHERA_DASHBOARD_TOKEN` gehören in die
> `.env`. Der Default aus diesem PR ist bereits der gewünschte Zustand; ab dem nächsten Start
> von `dashboard.py` ist die UI nur noch aus einer RDP-Sitzung auf der Box erreichbar.
>
> **D3 (cloudflared + Access) ist damit gestrichen**, nicht vertagt. Fernzugriff wird nicht
> gebraucht, und gegenüber „gar nicht erreichbar" vergrößert ein Tunnel die Angriffsfläche.
> Das Runbook in §5 bleibt als Referenz stehen und wird **nicht** ausgeführt.
>
> **Einziger Wiedervorlage-Auslöser:** die Z1-Quick-Actions (Audit-Punkt F4). Ein Live-Hebel in
> der Web-UI braucht einen Auth-Layer — kommt F4, kommt die Frage zurück. Sonst nicht.
>
> Vollzugs-Hinweis: `dashboard.py` steht **nicht** in `core/fleet.py` (eigene Scheduled Task).
> Der Marker-basierte Fleet-Restart erfasst es nicht; die Härtung greift erst beim nächsten
> Start dieses Prozesses.

Der Code-Teil ist fertig und für sich wirksam. Die ursprüngliche Optionsmatrix bleibt zur
Nachvollziehbarkeit stehen — entschieden ist **D1**:

### D1 — Loopback-only, kein Token (was nach dem Merge automatisch gilt)

* **Kosten:** 0. Keine Konfiguration, kein Restart über den ohnehin kommenden hinaus.
* **Gewonnen:** Der Listener ist nicht mehr an der öffentlichen Schnittstelle. Eine
  versehentliche Firewall-Allow-Regel exponiert nichts mehr. CSRF und Rebinding sind zu.
* **Restrisiko:** Jeder Prozess, der **auf der Box** als beliebiger Nutzer läuft, kann die
  Control-API weiterhin ohne Authentifizierung aufrufen (curl schickt keinen `Origin`, Host
  `localhost` steht auf der Allowlist). Das ist gegenüber heute unverändert. Kein
  Fernzugriff — das Dashboard ist nur per RDP-Sitzung erreichbar.

### D2 — D1 + Token (`KYTHERA_DASHBOARD_TOKEN` in der `.env`)

* **Kosten:** eine `.env`-Zeile (**Michi-Gate**, harte Regel 3) + ein Dashboard-Neustart.
  Bedienung danach: einmal `http://localhost:5000/?token=…` aufrufen, das Cookie trägt den Rest.
* **Gewonnen:** schließt das Restrisiko aus D1 — lokale Prozesse/Sitzungen ohne Token kommen
  nicht mehr an `stop_all`.
* **Restrisiko:** Der Token liegt im Klartext in der `.env` (wie alle anderen Secrets auch)
  und wird über Klartext-HTTP auf Loopback übertragen. Wer die `.env` lesen kann, hat ihn.

### D3 — Exposure: `cloudflared` + Cloudflare Access (der eigentliche Z2/B4-Scope, **nicht gebaut**)

* **Voraussetzung:** eigene Domain in Cloudflare (laut Task-Doc noch offen) **und** D2 —
  der Code verweigert den Start, wenn ein Off-Box-Hostname allowlisted ist und kein Token
  konfiguriert wurde.
* **Warum nicht in diesem PR:** Der Tunnel ist per Definition Exposure. Ein
  `cloudflared service install` auf der Live-Box, ein Access-Policy-Setup und ein
  Dashboard-Neustart sind allesamt Live-Eingriffe von genau der Klasse, die der Auftrag und
  `CLAUDE.md` ausschließen.
* **Was er bringt:** Fernzugriff (Handy/unterwegs) ohne offenen Port — die Verbindung ist
  outbound-only. Zugleich die harte Vorbedingung für die Z1-Quick-Actions (F4): ohne
  Auth-Layer kein Live-Hebel in der Web-UI.
* **Restrisiko, ehrlich beziffert:** Nach D3 hängt die Fleet-Stop-Fähigkeit an
  **zwei** Faktoren — der Access-Policy (falsch
  gescoped = weltweit offen, ein bekannter Fehlerfall bei Zero-Trust-Setups) und dem Token.
  Der Token ist der Grund, warum eine fehlkonfigurierte Access-Policy allein nicht reicht;
  erzwungen wird er durch die Startpolitik. Zusätzlich verlagert D3 Vertrauen zu Cloudflare
  (TLS-Terminierung beim Anbieter — für ein Ops-Dashboard vertretbar, für die `.env`-Secrets
  irrelevant, weil die nie über den Tunnel gehen).
* **Nicht abschätzbar ohne Live-Test:** ob `cloudflared` als Windows-Dienst mit der
  Watchdog-/Scheduled-Task-Landschaft der Box kollisionsfrei koexistiert. Die Erfahrung aus
  T-2026-CU-9050-170 (Z1-Dashboard-Task) sagt, dass lang laufende Dienste auf dieser Box unter
  S4U **nicht** binden und Passwort-Logon brauchen — das gilt für den Tunnel-Dienst mutmaßlich
  ebenso, ist aber ungeprüft.

**Empfehlung:** D2 beim nächsten ohnehin anstehenden Dashboard-Neustart mitnehmen (billig,
schließt das letzte lokale Loch). D3 erst entscheiden, wenn die Domain steht und der
Zeitpunkt für einen Live-Eingriff passt — der Sicherheitsgewinn von D3 gegenüber D2 ist
**negativ** (mehr Angriffsfläche), der Gewinn ist reiner Komfort plus die F4-Vorbedingung.
Das ist der Punkt, an dem die Reihenfolge „Z2 vor Z1" aus dem Task-Audit eine Begründung
braucht, die über „Absicherung zuerst" hinausgeht: **abgesichert ist das Dashboard nach D1/D2
auch ohne Tunnel.** Z2 ist Voraussetzung für die Z1-Quick-Actions, nicht für die Absicherung.

---

## 5. Runbook D3 (falls entschieden) — nicht ausgeführt

Nur zur Vorbereitung notiert; jeder Schritt ist ein Live-Eingriff.

1. `KYTHERA_DASHBOARD_TOKEN=<zufälliger 32-Byte-Wert>` in die `.env` (Michi).
2. `KYTHERA_DASHBOARD_ALLOWED_HOSTS=<tunnel-hostname>` in die `.env` — sonst antwortet der
   Guard dem Tunnel mit `403 host_not_allowed`.
3. Dashboard-Prozess neu starten; im Log muss `[token required]` stehen.
4. `cloudflared` installieren, Tunnel auf `http://127.0.0.1:5000` mappen, als Windows-Dienst
   registrieren (Logon-Typ analog T-2026-CU-9050-170 prüfen, siehe oben).
5. Cloudflare-Access-Policy **vor** dem ersten öffentlichen Aufruf setzen (ein Tunnel ohne
   Policy ist offen), Login-Policy für Michi; Service-Tokens später für Maschinen (Idee I9).
6. Verifizieren: (a) Tunnel-Hostname ohne Access-Login → abgewiesen; (b) mit Login, ohne
   Dashboard-Token → `401 token_invalid`; (c) mit beidem → UI; (d) `http://45.134.39.167:5000`
   von außen → unerreichbar (vorher erreichbar — das ist der Beleg, dass der Bind gegriffen
   hat, nicht eine Bestätigung des Ausgangszustands).

---

## 6. Korrektur der Aktenlage

Der Auftrag zu diesem Task nannte das Dashboard „den größten einzelnen DB-Lastverursacher der
Box". Das ist die Zuschreibung aus T-2026-CU-9050-166 (2026-07-19) und sie wurde bereits am
Folgetag von T-2026-CU-9050-179 korrigiert: der teuerste DB-Posten (`candles ⋈ indicators`,
~245 ms/Call) ist der **AI-Bot-Feature-Ladepfad** `core/candles.read_candles_with_indicators`,
belegt über `pg_stat_activity` (`user=dbfiller`).

Für **dieses** Dashboard ist die Frage ohnehin gegenstandslos: `dashboard.py` importiert
keinerlei DB-Code und stellt **null** Queries — schon der Audit-Report
(`audit_reports/10_dashboard_tools.md`, „Explicit non-findings") hielt das fest, und die
Import-Liste der Datei bestätigt es (`psutil`, `flask`, `core.fleet`, `core.process_control`).
Seine Last ist CPU (psutil-Sweeps), nicht DB. Das Z1-Shell-Dashboard
(`tools/dashboard/app.py`) wiederum liest ausschließlich DuckDB und nie Postgres — per
Modul-Invariante.

Die Sorge „eine Absicherung, die pro Request Queries erzeugt, verschlimmert ein bestehendes
Problem" ist trotzdem beantwortet, nur anders: der Guard erzeugt weder Queries noch
Prozess-Scans.
