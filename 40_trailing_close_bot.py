# 40_trailing_close_bot.py — Trailing-Close-Arm in einem eigenen Telegram-Channel.
"""
T-2026-KYT-9050-042 Phase C. Spiegelt die Signale der 33 in PR #198 ausgewählten
Beine (``core.trailing_roster``) in einen EIGENEN Channel und schließt sie dort per
Trailing-Close, statt sie bis SL/TP laufen zu lassen. Michi hängt Cornix an diesen
Channel — damit läuft der Trailing-Arm live gegen den Hold-Arm der bestehenden
Fleet, ohne dass ein einziger bestehender Bot sein Verhalten ändert.

Der Bot entscheidet NICHTS über Einstiege. Er spiegelt, was die Fleet ohnehin
postet, und trifft genau eine eigene Entscheidung: wann geschlossen wird.

Warum das ein eigener Prozess ist
---------------------------------
Der Trailing-Exit ist eine ANDERE Exit-Politik als die der Fleet. Ihn in die Bots
zu bauen hieße, den Hold-Arm zu zerstören, gegen den er gemessen werden soll. Als
eigener Prozess mit eigenem Channel ist er ein sauberer A/B-Arm: dieselben Entries,
zwei Exit-Regeln, zwei Kurven.

Datenfluss
----------
``ai_signals`` (fremd, NUR gelesen) → Roster+Register-Filter → Zulassung → Entry in
``telegram_outbox`` (eigener Channel) + eigene Zeile in ``trailing_positions`` →
Poll gegen Live-Preise → Trailing-Trigger ODER Quell-Trade verschwunden → ``Close
<SYMBOL>`` in denselben Channel.

Die drei Fallen, die diesen Bot formen
--------------------------------------
1. **Cornix' ``Close <SYMBOL>`` wirkt symbol-weit** (``core/config.py:123``). Zwei
   Positionen desselben Symbols im Channel heißt: der Trailing-Exit der einen macht
   die andere mit flach. ``28_signal_orchestrator.py:1562`` löst denselben Konflikt
   durch Zurückstellen des Close — hier wäre das falsch, weil der rechtzeitige Exit
   der ganze Zweck ist. Also: höchstens EINE Position je Symbol im Channel.
2. **Die gewählte Auswahl hat eine Belegungs-Spitze von 2001** = 4× den Cornix-Deckel
   (``trailing_slot_budget_live.md:82``). Ohne eigene Zulassungskontrolle entscheidet
   in der Spitze Cornix, welche ~1500 Trades abgelehnt werden. Der Bot deckelt
   deshalb selbst, und zwar nach Bein-Dichte statt nach Ankunftszeit.
3. **Ein skalenfreier Trail ist ein Micro-Scalper.** "10 % Rückgabe vom Peak" feuert
   auch auf einem 0,5-%-Peak. Die Aktivierungsschwelle (2 %, Operator) ist kein
   Tuning-Parameter, sondern die Bedingung dafür, dass der Bot Trades handelt und
   nicht Rauschen.

Preis-Kontrakt (Regel 5)
------------------------
Dieser Bot ist ein Monitor im Sinne der Ausnahme: er macht reine Preis-Checks gegen
den Live-Ticker (``core.live_price``, ein Binance-Call pro Poll für die ganze Fleet),
keine Indikator-Analyse. Er liest keine formende Kerze und leitet aus keiner Kerze
ein Signal ab.

Sicherheitsnetze
----------------
``TRAILING_BOT_LIVE_POSTING`` ist **default 0** und ``CH_TRAILING`` default ungesetzt:
ohne zwei bewusste Operator-Einträge läuft der Bot vollständig, trackt und loggt,
schreibt aber keine einzige Outbox-Zeile. Ein Deploy allein postet nichts.

Watchdog: start_delay=271.

Verlustbegrenzung (T-2026-KYT-9050-052, Operator-Entscheid Michi 2026-07-28)
----------------------------------------------------------------------------
Der Trail kann per Konstruktion nur Gewinner schließen — ohne Gegenstück entmischt
sich das Buch zu einem reinen Verlierer-Buch (live: 95 % unter Wasser in 9 h).
Deshalb zwei zusätzliche, strikt kausale Schranken (Zahlen: Verdikt
``staging_models/replay/trailing_arm_verdict_t052.md``):
  * **Zeit-Stop** (``TIME_STOP_H``, default 24 h): nie über die Aktivierungsschwelle
    gekommen → Close zum Markt, Grund ``TIME_STOP``.
  * **Exposure-Cap** (``EXPOSURE_CAP``, default ±50): eine Richtung darf der anderen
    höchstens 50 offene Spiegel voraus sein — neue Entries der Überhang-Richtung
    werden abgewiesen (kein Close, reine Zulassung).

Invariants:
  * Schreibt NIE in ``ai_signals`` und schließt NIE einen Fremd-Trade — sein einziges
    Schreibrecht sind ``telegram_outbox`` (eigener Channel) und ``trailing_positions``.
  * Höchstens eine offene Spiegel-Position je Symbol (Cornix-Close ist symbol-weit).
  * Offene Spiegel-Positionen ≤ ``SLOT_CAP``; Richtungs-Überhang ≤ ``EXPOSURE_CAP``.
  * Genau EINE Cornix-parsebare Nachricht je Entry (harte Regel 4).
  * Ein Bein ohne LIVE-Status in ``shadow_gate`` wird nie gespiegelt, auch wenn es
    im Roster steht.
  * Der Zeit-Stop entscheidet nur auf dem Peak-Stand von JETZT (kausal) und trifft
    nie einen scharfen Spiegel — der gehört dem Trail.
"""

import datetime
import json
import logging
import os
import time

from core import config as _kcfg
from core import shadow_gate
from core.database import PooledConnection, get_db_connection
from core.live_price import get_live_prices_batch
from core.market_utils import get_max_leverage
from core.signal_post import build_cornix_block
from core.trailing_roster import (
    ACTIVATION_PCT,
    EXPECTED_OCC_MEAN,
    EXPECTED_OCC_P95,
    RETRACE_FRAC,
    SLOT_CAP,
    SOURCE_REPORT,
    density,
    is_rostered,
    leg_key,
)
from core.trailing_state import TrailingState

logging.basicConfig(level=logging.INFO, format="%(asctime)s - TRAILING_BOT - %(message)s")
logger = logging.getLogger(__name__)

TARGET_CHANNEL_ID = _kcfg.CH_TRAILING
LIVE_POSTING = os.getenv("TRAILING_BOT_LIVE_POSTING", "0") == "1"
POLL_SECONDS = 10

# Wie alt darf ein Quell-Trade höchstens sein, damit ihn zu spiegeln noch DERSELBE
# Trade ist?
#
# Der Spiegel postet den Entry-Preis des Quell-Signals, und Cornix legt darauf eine
# Order, die erst füllt, wenn der Markt diesen Preis erreicht. Läuft der Markt in der
# Zwischenzeit weg, öffnet Cornix nie — während unser Buch die Position als offen
# führte und beim Trail ein `Close` ins Leere schickte. Am 2026-07-27 gemessen: bei
# einem 15-min-Fenster lagen 18 von 101 offenen Spiegeln mehr als 1 % vom Markt
# entfernt (Median 0,40 %, max 2,13 %).
#
# 30 s ist eng genug, dass der Entry praktisch am Markt liegt (der Poll läuft alle
# 10 s, ein frisches Signal wird also normalerweise binnen einer Runde gesehen), und
# weit genug, dass ein kurzer Aussetzer nichts verschluckt. Der Preis: Signale, die
# während eines Fleet-Restarts (~5 min) aufgehen, fallen weg — bewusst, denn ein
# Trade, den Cornix nie öffnet, ist schlimmer als einer, den wir auslassen.
MAX_MIRROR_AGE_SEC = float(os.getenv("TRAILING_BOT_MAX_AGE_SEC", "30"))

# Wie lange auf den Fill gewartet wird, bevor die Order als verfallen gilt. Danach
# wird die Zeile geschlossen und ein `Close` gepostet, damit in Cornix keine
# Alt-Order liegen bleibt, die Tage später doch noch füllt.
FILL_TIMEOUT_MIN = float(os.getenv("TRAILING_BOT_FILL_TIMEOUT_MIN", "10"))

# Zeit-Stop (T-2026-KYT-9050-052, Operator-Entscheid Michi 2026-07-28): ein Spiegel,
# dessen Peak binnen dieser Frist nie über die Aktivierungsschwelle kam, wird zum
# Markt geschlossen. Begründung aus dem Buch-Gesundheits-Verdikt: der Trail kann per
# Konstruktion nur Gewinner schließen — die Nie-Scharfen liegen sonst bis zum
# Katastrophen-SL (live gemessen: SOURCE_CLOSED Ø −4,8 %, 34 SL-Hits an einem Tag),
# und das Buch entmischt sich zu einem reinen Verlierer-Buch (95 % unter Wasser in
# 9 h). Der Stop verkauft Erholungen — das ist sein simulierter, akzeptierter Preis
# (−11k auf 5 Monate gegen MaxDD −31 % und halbe Slot-Bindung).
TIME_STOP_H = float(os.getenv("TRAILING_BOT_TIME_STOP_H", "24"))

# Flutschutz: höchstens so viele Zeit-Stop-Closes je 10s-Zyklus. Nach einem Restart
# mit Altbestand wären sonst ~150 `Close`-Kommandos in EINEM Zyklus in der Outbox —
# der Telegram-Sender arbeitet FIFO, der Stau würde alle anderen Fleet-Messages
# verzögern. So verteilt sich die Bereinigung über einige Minuten.
TIME_STOP_MAX_PER_CYCLE = int(os.getenv("TRAILING_BOT_TIME_STOP_MAX_PER_CYCLE", "25"))

# Netto-Exposure-Deckel je Richtung (T-2026-KYT-9050-052): ein neuer Entry, dessen
# Richtung bereits EXPOSURE_CAP Positionen vor der Gegenrichtung liegt, wird nicht
# aufgenommen. Kein Marktlagen-Modell (die wurden gemessen und verworfen), sondern
# eine strukturelle Schranke: das Buch darf nicht beliebig einseitig werden. 0 = aus.
EXPOSURE_CAP = int(os.getenv("TRAILING_BOT_EXPOSURE_CAP", "50"))

# Wie lange ein Symbol nach einem GEPOSTETEN Close gesperrt bleibt, bevor es neu
# belegt werden darf.
#
# Die Outbox liefert je Channel streng FIFO (4_telegram_bot.py, P0.1(d)), Cornix
# bekommt `Close X` also garantiert vor dem neuen Entry auf X. Das reicht aber nur
# bis zur Telegram-Grenze: Cornix setzt daraufhin ZWEI Markt-Orders in Gegenrichtung
# fast gleichzeitig an Binance ab. Ist der Close dort noch nicht abgerechnet, wenn
# die Gegenposition aufgeht, macht der nachlaufende Close die neue Position gleich
# wieder flach. Gemessen am 2026-07-27: XTZUSDT Close + neuer Entry in DERSELBEN
# Sekunde (SHORT → LONG), ENAUSDT 3 s auseinander (LONG → SHORT). Nichts davon ist
# schiefgegangen — aber das war Glück, nicht Konstruktion.
#
# Der Preis sind ein paar verspätete Einstiege; bei 33 Beinen auf ~530 Coins sind
# Symbol-Kollisionen häufig, die Sperre verschiebt sie nur.
SYMBOL_COOLDOWN_SEC = float(os.getenv("TRAILING_BOT_SYMBOL_COOLDOWN_SEC", "60"))

# Exit-Gründe (landen in trailing_positions.close_reason)
REASON_TRAIL = "TRAIL"
#: Zeit-Stop: nie über die Aktivierungsschwelle gekommen und älter als TIME_STOP_H.
REASON_TIME_STOP = "TIME_STOP"
REASON_SOURCE_CLOSED = "SOURCE_CLOSED"
REASON_LEG_RETIRED = "LEG_RETIRED"
#: Kein Exit, sondern ein Vermerk: dieser Quell-Trade lief schon, als der Bot
#: startete. Er wird nie gespiegelt — die Zeile existiert nur, damit er auch nie
#: wieder als Neuzugang erscheint (dieselbe Sperre wie ein geschlossener Spiegel).
REASON_PREEXISTING = "PREEXISTING"
#: Beim Umschalten von Shadow auf Live geschlossen: die Zeile war offen, aber nie
#: veröffentlicht, kann also keiner Position im Channel entsprechen.
REASON_SHADOW_CARRYOVER = "SHADOW_CARRYOVER"
#: Cornix hat den Entry nie erreicht — die Order verfiel, ohne je zu füllen.
REASON_NOT_FILLED = "ENTRY_NOT_FILLED"
#: Der SL wurde erreicht — Cornix schließt die Position selbst über die Order auf der
#: Börse. Wir buchen den Exit nur nach und posten BEWUSST NICHTS: ein zusätzliches
#: `Close` wäre bestenfalls überflüssig und behauptete einen Exit, den nicht wir
#: ausgelöst haben (Operator-Hinweis Michi, 2026-07-27).
REASON_SL_HIT = "SL_HIT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trailing_positions (
    id             BIGSERIAL PRIMARY KEY,
    src_signal_id  BIGINT      NOT NULL,
    symbol         VARCHAR(20) NOT NULL,
    model          TEXT        NOT NULL,
    direction      VARCHAR(10) NOT NULL,
    entry          DOUBLE PRECISION NOT NULL,
    peak_pct       DOUBLE PRECISION,
    opened_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at      TIMESTAMPTZ,
    close_reason   TEXT,
    close_mark_pct DOUBLE PRECISION,
    posted         BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS trailing_positions_src_uniq
    ON trailing_positions (src_signal_id);
CREATE UNIQUE INDEX IF NOT EXISTS trailing_positions_open_symbol_uniq
    ON trailing_positions (symbol) WHERE closed_at IS NULL;
"""

# Additiv nachgezogen (T-2026-KYT-9050-050): CREATE TABLE IF NOT EXISTS rührt eine
# bestehende Tabelle nicht an, die Spalten müssen also einzeln kommen — dasselbe
# Muster wie die Schema-Sicherung in 8_ai_trade_monitor.
SCHEMA_ADD = [
    "ALTER TABLE trailing_positions ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ",
    "ALTER TABLE trailing_positions ADD COLUMN IF NOT EXISTS mirror_price DOUBLE PRECISION",
    "ALTER TABLE trailing_positions ADD COLUMN IF NOT EXISTS sl DOUBLE PRECISION",
]


def ensure_schema(conn) -> None:
    """Eigene Tabelle anlegen. Rührt keine bestehende Fleet-Tabelle an.

    Der partielle Unique-Index auf ``symbol WHERE closed_at IS NULL`` ist die
    Symbol-Eindeutigkeit als DB-Zusicherung, nicht nur als Code-Prüfung: zwei
    Positionen desselben Symbols wären eine stille Fehl-Schließung durch Cornix'
    symbol-weites ``Close``, und dagegen ist ein Constraint das ehrlichere Mittel
    als eine Bedingung, die ein späterer Refactor wegoptimiert.
    """
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        for stmt in SCHEMA_ADD:
            cur.execute(stmt)
    conn.commit()


def clear_unposted_carryover(conn) -> int:
    """Offene Spiegel-Zeilen ohne Veröffentlichung schließen — nur im Live-Modus.

    Eine offene Zeile mit ``posted = FALSE`` kann keiner Position im Channel
    entsprechen: sie wurde nie gepostet. Im Shadow-Betrieb ist das der Normalfall
    und muss stehen bleiben (es IST das Shadow-Buch). Sobald aber live gepostet
    wird, ist so eine Zeile Altlast aus der Shadow-Phase — und eine schädliche:
    sie belegt ihr Symbol (höchstens eine Position je Symbol) und einen Slot,
    beides für etwas, das es im Channel nicht gibt. Beim Umschalten am 2026-07-26
    waren das 460 Zeilen, also 460 blockierte Symbole.

    Läuft nur beim Start, nicht im Poll: im laufenden Live-Betrieb entstehen
    unveröffentlichte offene Zeilen gar nicht (Insert und Outbox-Zeilen liegen in
    derselben Transaktion), ein Aufräumen im Zyklus hätte also nichts zu tun und
    könnte nur schaden.
    """
    if not (LIVE_POSTING and TARGET_CHANNEL_ID):
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trailing_positions
            SET closed_at = NOW(), close_reason = %s
            WHERE closed_at IS NULL AND posted = FALSE
            """,
            (REASON_SHADOW_CARRYOVER,),
        )
        n = cur.rowcount
    conn.commit()
    if n:
        logger.info("🧹 %d unveröffentlichte Shadow-Zeile(n) geschlossen — Symbole/Slots freigegeben.", n)
    return n


# ─────────────────────────────────────────────────────────────────────────────
# LESEN (fremde Tabelle — ausschliesslich SELECT)
# ─────────────────────────────────────────────────────────────────────────────


def read_source_signals(conn) -> tuple[dict[int, dict], set[int]]:
    """``(spiegelbare Quell-Trades, ids ALLER offenen Quell-Trades)``.

    ``ai_signals`` ist die Tabelle des AI-Monitors (Bot 8). Sie wird hier nur
    gelesen; der Monitor bleibt ihr einziger Schreiber.

    Die zweite Menge trennt zwei Fälle, die sonst gleich aussähen und es nicht
    sind: ein Quell-Trade, den die Fleet geschlossen hat (Zeile weg), und einer,
    der noch läuft, aber durch Roster/Register herausgefallen ist. Beide beenden
    den Spiegel — mit unterschiedlichem Grund im Protokoll.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, symbol, model, direction, entry1, price, sl, targets, lev,
                   EXTRACT(EPOCH FROM (NOW() - open_time)) AS age_sec
            FROM ai_signals
            """
        )
        rows = cur.fetchall()

    out: dict[int, dict] = {}
    all_open: set[int] = set()
    # Das Alter rechnet die DB, nicht Python: `ai_signals.open_time` ist naiv und
    # wird PG-lokal geschrieben (TZ-Kontrakt R3). Ein Vergleich gegen eine in
    # Python gebildete "jetzt"-Zeit wäre genau der Offset-Fehler aus dem TZ-Cluster
    # P2.1–P2.6; `NOW() - open_time` kann ihn gar nicht erst machen.
    for sid, symbol, model, direction, entry1, price, sl, targets, lev, age_sec in rows:
        all_open.add(int(sid))
        if not is_rostered(model, direction):
            continue
        tag, side = leg_key(model, direction)
        # Register schlägt Roster: der Roster ist ein Standbild vom 2026-07-26,
        # shadow_gate ist der lebende Zustand. Ein zwischenzeitlich abgeschaltetes
        # Bein darf nicht weiter in einen Live-Channel gespiegelt werden.
        if not shadow_gate.is_live(tag, side):
            continue
        entry = float(entry1) if entry1 is not None else (float(price) if price is not None else None)
        if entry is None or entry <= 0:
            continue
        tgt = json.loads(targets) if isinstance(targets, str) else targets
        if sl is None or not tgt:
            # Ohne SL oder Targets liesse sich kein vollständiger Cornix-Block
            # bauen — eine halbe Order-Geometrie in einen Cornix-Channel zu posten
            # wäre schlimmer als nicht zu spiegeln.
            logger.warning(f"⚠️ {symbol} ({model} {direction}): kein SL/Target — nicht gespiegelt.")
            continue
        out[int(sid)] = {
            "symbol": symbol,
            "model": model,
            "tag": tag,
            "direction": side,
            "entry": entry,
            "sl": float(sl) if sl is not None else None,
            "targets": [float(t) for t in (tgt or [])],
            "lev": lev,
            "density": density(model, direction),
            # None (open_time NULL) gilt als beliebig alt — im Zweifel nicht spiegeln.
            "age_sec": float(age_sec) if age_sec is not None else float("inf"),
        }
    return out, all_open


def read_mirrored_src_ids(conn, src_ids: set[int]) -> set[int]:
    """Welche dieser Quell-Trades hat der Bot schon einmal gespiegelt — offen ODER
    bereits geschlossen?

    Das ist die Sperre gegen den Wiedereinstieg, und der Fall ist der Normalfall,
    nicht der Ausnahmefall: der Trailing-Exit feuert typischerweise, WÄHREND der
    Quell-Trade noch läuft (genau dafür existiert der Bot). Gegen nur die offenen
    Spiegel geprüft, sähe dieselbe `ai_signals`-Zeile beim nächsten Poll wieder
    wie ein neues Signal aus — und der Bot würde alle 10 s neu eröffnen, bis die
    Fleet den Quell-Trade schließt.

    Abgefragt wird gegen die aktuell offenen Quell-ids statt gegen die ganze
    Tabelle, damit die Prüfung mit der Zahl offener Trades skaliert und nicht mit
    der Historie.
    """
    if not src_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT src_signal_id FROM trailing_positions WHERE src_signal_id = ANY(%s)",
            (list(src_ids),),
        )
        return {int(r[0]) for r in cur.fetchall()}


def read_cooling_symbols(conn) -> set[str]:
    """Symbole, für die gerade ein `Close` bei Cornix in Arbeit sein könnte.

    Nur GEPOSTETE Closes kühlen ab: ein Shadow-Close hat nie ein Kommando gesendet
    und kann deshalb mit nichts rennen. Das Fenster rechnet die DB gegen ihre eigene
    ``NOW()`` — dieselbe Begründung wie beim Altersfilter (TZ-Kontrakt R3).
    """
    if SYMBOL_COOLDOWN_SEC <= 0:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT symbol FROM trailing_positions
            WHERE posted AND closed_at IS NOT NULL
              AND closed_at > NOW() - make_interval(secs => %s)
            """,
            (SYMBOL_COOLDOWN_SEC,),
        )
        return {r[0] for r in cur.fetchall()}


def read_open_mirrors(conn) -> dict[int, dict]:
    """Eigene offene Spiegel-Positionen, ``src_signal_id`` → Zeile."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, src_signal_id, symbol, model, direction, entry, peak_pct, posted,
                   filled_at, mirror_price, opened_at, sl
            FROM trailing_positions
            WHERE closed_at IS NULL
            """
        )
        rows = cur.fetchall()
    return {
        int(src): {
            "id": int(rid),
            "symbol": symbol,
            "model": model,
            "direction": direction,
            "entry": float(entry),
            "peak_pct": float(peak) if peak is not None else None,
            "posted": bool(posted),
            # Alt-Zeilen (vor T-050) tragen keine mirror_price. Sie liefen unter der
            # alten Logik bereits als offen und werden weiter so behandelt — sie
            # nachträglich als ungefüllt zu erklären hieße, ~100 Live-Positionen auf
            # einen Verdacht hin stillzulegen.
            "filled": filled is not None or mprice is None,
            "mirror_price": float(mprice) if mprice is not None else None,
            "opened_at": opened,
            "sl": float(sl) if sl is not None else None,
        }
        for rid, src, symbol, model, direction, entry, peak, posted, filled, mprice, opened, sl in rows
    }


# ─────────────────────────────────────────────────────────────────────────────
# POSTEN (eigener Channel — genau EINE parsebare Nachricht pro Entry)
# ─────────────────────────────────────────────────────────────────────────────


def _post(conn, message: str) -> None:
    """Outbox-Zeile für den eigenen Channel. Committet nicht (Caller-Kontrakt)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
            (TARGET_CHANNEL_ID, message),
        )


def entry_messages(sig: dict) -> tuple[str, str]:
    """(Cornix-Block, HTML-Info) für einen gespiegelten Entry.

    Der Cornix-Block kommt aus ``core.signal_post.build_cornix_block`` — derselben
    Quelle, aus der die Fleet postet. Die Info-Nachricht wiederholt ihn bewusst
    NICHT: zwei parsebare Nachrichten wären zwei Positionen (harte Regel 4, der
    fleet-weite Doppel-Trade-Bug vom 2026-07-06).
    """
    lev = sig["lev"] or get_max_leverage(sig["symbol"], 20)
    cornix = build_cornix_block(
        model_tag=f"{sig['tag']}-TRAIL",
        symbol=sig["symbol"],
        direction=sig["direction"],
        lev=lev,
        entry1=sig["entry"],
        sl=sig["sl"],
        targets=sig["targets"],
    )
    info = (
        "<pre>"
        + "\n".join(
            [
                f"<b>🪝 TRAILING MIRROR — {sig['tag']} {sig['direction']}</b>",
                f"<b>{sig['symbol']}</b>",
                f"<b>→ Trail: {RETRACE_FRAC:.0%} give-back once peak &gt; {ACTIVATION_PCT:.1f}%</b>",
                f"<b>→ Leg density: {sig['density']:.3f} % / slot-day</b>",
                f"<b>→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC</b>",
            ]
        )
        + "</pre>"
    )
    return cornix, info


def close_messages(row: dict, reason: str, mark: float | None) -> tuple[str, str]:
    """(Close-Kommando, HTML-Info) für einen Exit.

    ``Close <SYMBOL>`` ist Cornix' Schließ-Kommando (``core/config.py:123``) und
    trifft ALLE Trades des Symbols im Channel — der Bot hält deshalb nie zwei
    Positionen auf einem Symbol. Das Kommando enthält keine Entry-Felder und ist
    damit nicht als neues Signal parsebar.
    """
    if reason == REASON_TRAIL:
        why = "trailing stop"
    elif reason == REASON_TIME_STOP:
        why = f"time stop ({TIME_STOP_H:.0f}h below +{ACTIVATION_PCT:.0f}% activation)"
    else:
        why = "source trade closed"
    info = (
        "<pre>"
        + "\n".join(
            [
                f"<b>🔒 TRAILING CLOSE — {row['model']} {row['direction']}</b>",
                f"<b>{row['symbol']}</b>",
                f"<b>→ Reason: {why}</b>",
                f"<b>→ Mark: {'n/a' if mark is None else f'{mark:+.2f}%'} (unlevered)</b>",
            ]
        )
        + "</pre>"
    )
    return f"Close {row['symbol']}", info


# ─────────────────────────────────────────────────────────────────────────────
# ZULASSUNG
# ─────────────────────────────────────────────────────────────────────────────


def admit(
    candidates: list[tuple[int, dict]],
    held_symbols: set[str],
    free_slots: int,
    cooling: set[str] | None = None,
    open_by_dir: dict[str, int] | None = None,
) -> tuple[list, list]:
    """Wer darf in den Channel? Gibt ``(zugelassen, abgewiesen_mit_grund)`` zurück.

    Vier Gründe, alle hart:
      * ``SYMBOL_HELD`` — auf dem Symbol läuft schon eine Spiegel-Position, und
        Cornix' Close ist symbol-weit.
      * ``SYMBOL_COOLING`` — auf dem Symbol wurde gerade ein `Close` gepostet, das
        bei Cornix noch in Arbeit sein kann.
      * ``EXPOSURE_CAP`` — die Richtung des Kandidaten liegt schon ``EXPOSURE_CAP``
        offene Positionen vor der Gegenrichtung. Das Buch darf nicht beliebig
        einseitig werden (T-052: das einseitige LONG-Buch WAR der Konto-Schaden;
        die strukturelle Schranke schlug in der Messung jedes Marktlagen-Modell).
      * ``SLOT_CAP`` — der Channel ist voll. Sortiert wird nach Bein-Dichte, damit
        bei Knappheit dasselbe Kriterium entscheidet, das die Auswahl überhaupt
        getroffen hat: Ertrag je belegtem Slot-Tag.

    ``open_by_dir`` sind die BEREITS offenen Spiegel je Richtung; zugelassene
    Kandidaten zählen sofort mit, damit ein einzelner Zyklus den Deckel nicht
    überrennen kann. Abweisungen werden zurückgegeben, nicht verschluckt — eine
    stille Deckelung liest sich später wie "alles gespiegelt".
    """
    admitted, rejected = [], []
    taken = set(held_symbols)
    cooling = cooling or set()
    dir_cnt = {"LONG": 0, "SHORT": 0, **(open_by_dir or {})}
    for sid, sig in sorted(candidates, key=lambda c: -c[1]["density"]):
        if sig["symbol"] in taken:
            rejected.append((sid, sig, "SYMBOL_HELD"))
            continue
        if sig["symbol"] in cooling:
            # Auf diesem Symbol ist ein Close unterwegs — ihn mit einer Gegenorder
            # zu überholen ist genau das Rennen, das wir nicht eingehen.
            rejected.append((sid, sig, "SYMBOL_COOLING"))
            continue
        d = sig["direction"]
        other = "SHORT" if d == "LONG" else "LONG"
        if EXPOSURE_CAP > 0 and dir_cnt.get(d, 0) - dir_cnt.get(other, 0) >= EXPOSURE_CAP:
            rejected.append((sid, sig, "EXPOSURE_CAP"))
            continue
        if len(admitted) >= free_slots:
            rejected.append((sid, sig, "SLOT_CAP"))
            continue
        taken.add(sig["symbol"])
        dir_cnt[d] = dir_cnt.get(d, 0) + 1
        admitted.append((sid, sig))
    return admitted, rejected


# ─────────────────────────────────────────────────────────────────────────────
# EIN POLL-ZYKLUS
# ─────────────────────────────────────────────────────────────────────────────


def record_preexisting(conn, stale: list[tuple[int, dict]]) -> None:
    """Quell-Trades als „gesehen, nie gespiegelt" vermerken.

    Die Zeile wird sofort geschlossen eingetragen (``closed_at = NOW()``): sie ist
    kein Spiegel, sondern eine Sperre. ``read_mirrored_src_ids`` fragt ohne
    ``closed_at``-Filter, also taucht dieser Quell-Trade nie wieder als Neuzugang
    auf — dieselbe Mechanik, die einen ausgetrailten Trade vor dem Wiedereinstieg
    schützt. Ein geschlossener Eintrag kollidiert auch nicht mit dem partiellen
    Symbol-Index (der greift nur auf offenen Zeilen), belegt also keinen Platz.
    """
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO trailing_positions
                (src_signal_id, symbol, model, direction, entry, peak_pct, posted,
                 closed_at, close_reason)
            VALUES (%s, %s, %s, %s, %s, NULL, FALSE, NOW(), %s)
            ON CONFLICT DO NOTHING
            """,
            [
                (sid, sig["symbol"], sig["model"], sig["direction"], sig["entry"], REASON_PREEXISTING)
                for sid, sig in stale
            ],
        )
    conn.commit()
    oldest = max(sig["age_sec"] for _sid, sig in stale)
    logger.info(
        "📎 %d Quell-Trade(s) als Altbestand vermerkt, nicht gespiegelt (ältester %.0f s, Grenze %.0f s).",
        len(stale),
        oldest,
        MAX_MIRROR_AGE_SEC,
    )


def open_mirrors(
    conn,
    sources: dict[int, dict],
    mirrors: dict[int, dict],
    already: set[int],
    prices: dict[str, float] | None = None,
) -> int:
    """Neue Quell-Signale spiegeln. Gibt die Zahl der eröffneten Positionen zurück.

    ``already`` sind die Quell-ids, die der Bot schon einmal gespiegelt hat —
    offen ODER geschlossen. Gegen die offenen Spiegel allein zu prüfen wäre der
    Wiedereinstiegs-Bug: nach einem Trailing-Exit läuft der Quell-Trade meist
    weiter, seine Zeile sähe wieder neu aus, und der Bot würde alle 10 s neu
    eröffnen. Ein einmal getrailter Trade ist erledigt.
    """
    unseen = [(sid, sig) for sid, sig in sources.items() if sid not in mirrors and sid not in already]
    if not unseen:
        return 0

    # Altbestand: lief schon, bevor der Bot ihn sehen konnte. Wird NICHT gespiegelt,
    # aber als Zeile vermerkt, damit er nie wieder als Neuzugang auftaucht.
    stale = [(sid, sig) for sid, sig in unseen if sig["age_sec"] > MAX_MIRROR_AGE_SEC]
    if stale:
        record_preexisting(conn, stale)
    new = [(sid, sig) for sid, sig in unseen if sig["age_sec"] <= MAX_MIRROR_AGE_SEC]
    if not new:
        return 0

    held = {m["symbol"] for m in mirrors.values()}
    open_by_dir = {"LONG": 0, "SHORT": 0}
    for m in mirrors.values():
        open_by_dir[m["direction"]] = open_by_dir.get(m["direction"], 0) + 1
    admitted, rejected = admit(new, held, SLOT_CAP - len(mirrors), read_cooling_symbols(conn), open_by_dir)

    # Gebündelt statt je Kandidat: die Abweisungen wiederholen sich in JEDEM
    # 10s-Zyklus, solange der Quell-Trade offen ist. Im ersten Shadow-Lauf waren
    # das ~870 Zeilen pro Zyklus = ~1,5 Mio/Tag in den gemeinsamen Watchdog-Log —
    # die Logs aller anderen Bots wären darin ertrunken. Die Zahlen bleiben
    # sichtbar (keine stille Deckelung), die Einzelfälle stehen auf DEBUG.
    if rejected:
        tally: dict[str, int] = {}
        for _sid, _sig, why in rejected:
            tally[why] = tally.get(why, 0) + 1
        logger.info(
            "⛔ %d nicht aufgenommen (%s)", len(rejected), ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))
        )
        for _sid, sig, why in rejected:
            logger.debug("⛔ %s %s %s: %s", sig["symbol"], sig["tag"], sig["direction"], why)

    opened = 0
    live = bool(LIVE_POSTING and TARGET_CHANNEL_ID)
    prices = get_live_prices_batch() if prices is None else prices
    for sid, sig in admitted:
        # Der Marktpreis im Moment des Spiegelns entscheidet, von welcher Seite der
        # Entry erreicht werden muss. Ohne ihn liesse sich der Fill nicht feststellen,
        # also lieber diesen Zyklus auslassen — das Signal ist im 90s-Fenster noch da.
        market = prices.get(sig["symbol"])
        if market is None:
            logger.info("⏸ %s: kein Marktpreis beim Spiegeln — nächster Zyklus.", sig["symbol"])
            continue
        if not mirrorable_at(sig["direction"], market, sig["sl"], sig["targets"]):
            logger.info(
                "⛔ %s %s %s: Markt %s liegt ausserhalb SL/TP1 — nicht mehr spiegelbar.",
                sig["symbol"],
                sig["tag"],
                sig["direction"],
                market,
            )
            continue
        # ENTRY = MARKT (Operator-Entscheid Michi, 2026-07-27). Vorher wurde der Entry
        # des Quell-Signals gepostet — bis der Bot spiegelt, hat die auslösende
        # Bewegung aber stattgefunden, und der Markt kam zu diesem Preis meist nicht
        # zurück: von 24 Spiegeln füllten 5 (21 %), bei 15 der 18 Stornos hatte der
        # Markt den Entry laut 5m-Kerzen nie berührt. Der Arm handelte damit eine
        # Auswahl, die er selbst erzeugt — bevorzugt Trades, deren Bewegung sich
        # zurückbildet. Zum Markt einzusteigen füllt praktisch immer und lässt beide
        # Arme dieselben Signale handeln.
        sig["entry"] = market
        sig["mirror_price"] = market
        # SCHREIBEN ZUERST, posten nur bei echtem Insert — dasselbe Muster wie
        # `DELETE ... RETURNING` im AI-Monitor (P2.8). Anders herum wäre die
        # Outbox-Zeile schon geschrieben, wenn der Insert am Unique-Index
        # scheitert (Symbol schon belegt, Quelle schon gespiegelt, zweiter
        # Prozess) — und ein Post ohne zugehörige Zeile ist eine Position, die
        # niemand mehr schliesst.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trailing_positions
                    (src_signal_id, symbol, model, direction, entry, peak_pct, posted,
                     mirror_price, filled_at, sl)
                VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, NOW(), %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    sid,
                    sig["symbol"],
                    sig["model"],
                    sig["direction"],
                    sig["entry"],
                    live,
                    sig.get("mirror_price"),
                    sig["sl"],
                ),
            )
            created = cur.fetchone()
        if created is None:
            conn.rollback()
            logger.warning(f"⚠️ {sig['symbol']} {sig['tag']} {sig['direction']}: Insert verloren — nicht gepostet.")
            continue
        if live:
            cornix, info = entry_messages(sig)
            _post(conn, cornix)
            _post(conn, info)
        conn.commit()
        opened += 1
        logger.info(
            f"🪝 Mirror{'' if live else ' [SHADOW]'}: {sig['symbol']} {sig['tag']} {sig['direction']} @ {sig['entry']}"
        )
    return opened


def close_mirror(conn, row: dict, reason: str, mark: float | None, post: bool = True) -> None:
    """Spiegel-Position schließen: Close-Kommando + eigene Zeile stempeln.

    ``post=False`` für Exits, die Cornix selbst ausgelöst hat (SL) — dort wäre ein
    eigenes `Close` überflüssig, und es würde einen Exit behaupten, den wir nicht
    verursacht haben.
    """
    cmd, info = close_messages(row, reason, mark)
    if post and LIVE_POSTING and TARGET_CHANNEL_ID and row["posted"]:
        # Nur schließen, was auch eröffnet wurde. Ein `Close` auf eine nie gepostete
        # Position wäre im Live-Channel ein Kommando gegen einen fremden Trade.
        _post(conn, cmd)
        _post(conn, info)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trailing_positions
            SET closed_at = NOW(), close_reason = %s, close_mark_pct = %s
            WHERE id = %s AND closed_at IS NULL
            """,
            (reason, mark, row["id"]),
        )
    conn.commit()
    logger.info(
        "🔒 Close %s (%s %s) — %s @ %s",
        row["symbol"],
        row["model"],
        row["direction"],
        reason,
        "n/a" if mark is None else f"{mark:+.2f}%",
    )


def mirrorable_at(direction: str, market: float, sl: float, targets: list[float]) -> bool:
    """Ergibt ein Markt-Einstieg auf dieser Geometrie überhaupt noch Sinn?

    Der Spiegel steigt zum aktuellen Markt ein, behält aber SL und Targets des
    Quell-Signals auf ihren ABSOLUTEN Preisen — es sind S/R-Level, sie mitzuschieben
    löste sie von den Leveln, und der SL soll derselbe Katastrophen-Stop sein wie im
    Hold-Arm. Genau deshalb braucht es diesen Riegel: ist der Markt schon über TP1
    hinaus, wäre die Position im selben Moment im Ziel; ist er jenseits des SL, wäre
    sie sofort ausgestoppt. Beides ist kein Trade, sondern eine Gebühr.
    """
    if not targets or sl is None:
        return False
    tp1 = targets[0]
    if direction == "LONG":
        return sl < market < tp1
    return tp1 < market < sl


def sl_reached(direction: str, sl: float | None, price: float) -> bool:
    """Hat der Markt den Stop-Loss erreicht?

    Cornix hält den SL als Order auf der Börse und schließt selbst. Wir erkennen es
    nur, um unser Buch nachzuziehen — und um KEIN eigenes `Close` zu senden.
    """
    if sl is None:
        return False
    return price <= sl if direction == "LONG" else price >= sl


def has_filled(entry: float, mirror_price: float, price: float) -> bool:
    """Hat der Markt den Entry seit dem Spiegeln erreicht?

    Cornix legt auf den geposteten Entry eine Order und öffnet erst, wenn der Markt
    dort handelt — richtungsunabhängig, wie Michi am 2026-07-27 beobachtet hat
    (ENAUSDT SHORT, Entry 1,5 % unter Markt, blieb ungeöffnet). Die Prüfung bildet
    genau das ab: der Preis muss den Entry von der Seite erreichen, auf der er beim
    Spiegeln stand. Sie trifft KEINE Annahme darüber, wie Cornix LONG und SHORT
    unterschiedlich behandelt — nur darüber, dass der Preis den Entry berühren muss.
    """
    if mirror_price >= entry:
        return price <= entry
    return price >= entry


def poll_open_mirrors(
    conn,
    sources: dict[int, dict],
    mirrors: dict[int, dict],
    all_open: set[int] | None = None,
    prices: dict[str, float] | None = None,
) -> None:
    """Preis-Poll über alle offenen Spiegel-Positionen (Trailing + Quell-Close)."""
    if not mirrors:
        return
    if prices is None:
        prices = get_live_prices_batch()
    open_ids = all_open if all_open is not None else set(sources)
    missing = 0
    unfilled = 0
    time_stopped = 0

    for sid, row in mirrors.items():
        if sid not in sources:
            # Zwei verschiedene Sachverhalte, beide beenden den Spiegel:
            #   * Zeile weg  → der AI-Monitor hat den Quell-Trade geschlossen
            #     (SL/TP/Timeout). Der Spiegel darf keine Position halten, die die
            #     Quell-Strategie nicht mehr hält — sonst misst der A/B-Arm nicht
            #     mehr dieselben Trades.
            #   * Zeile da, aber gefiltert → das Bein ist aus Roster/Register
            #     gefallen. Auch dann hört das Spiegeln auf, aber aus einem anderen
            #     Grund, und das gehört unterscheidbar ins Protokoll.
            reason = REASON_LEG_RETIRED if sid in open_ids else REASON_SOURCE_CLOSED
            price = prices.get(row["symbol"])
            mark = None
            if price is not None:
                st = TrailingState(row["entry"], row["direction"] == "LONG", RETRACE_FRAC, ACTIVATION_PCT)
                mark = st.update(float(price))[1]
            # Ohne Preis wird trotzdem geschlossen — die Quelle hält die Position
            # nicht mehr, Halten wäre also falsch. Der Mark bleibt dann NULL statt
            # einer erfundenen 0.0: das Buch soll keinen Wert behaupten, den
            # niemand gemessen hat.
            close_mirror(conn, row, reason, mark)
            continue

        price = prices.get(row["symbol"])
        if price is None:
            # Kein Preis heißt keine Entscheidung. Eine Position auf einem Coin ohne
            # Tick bleibt offen — sie zu schließen wäre eine Aussage über einen Markt,
            # den wir gerade nicht sehen.
            #
            # BEWUSST OHNE Einzelabfrage-Fallback (Operator-Auftrag Michi, 2026-07-26):
            # `core.live_price.get_live_price` würde einen HTTP-Call PRO Position PRO
            # Poll machen. Bei den für act=2 % erwarteten ~285 gleichzeitigen
            # Positionen im 10s-Takt sind das ~28 Requests/s gegen fapi.binance.com —
            # ein Ban kostet die ganze Fleet, ein um 10 s verzögerter Trailing-Exit
            # kostet fast nichts. Der nächste Poll versucht den Batch erneut.
            missing += 1
            continue

        if row["filled"] and sl_reached(row["direction"], row["sl"], float(price)):
            # Cornix hat hier schon geschlossen. Nur nachbuchen, nichts posten.
            close_mirror(conn, row, REASON_SL_HIT, None, post=False)
            continue

        if not row["filled"]:
            # Noch nicht gefüllt: Cornix hat die Position nicht offen, also gibt es
            # nichts zu trailen. Ein Trail auf einer ungefüllten Order erzeugt genau
            # den Phantom-Exit, den dieser Task behebt.
            if has_filled(row["entry"], row["mirror_price"], float(price)):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE trailing_positions SET filled_at = NOW() WHERE id = %s AND filled_at IS NULL",
                        (row["id"],),
                    )
                conn.commit()
                row["filled"] = True
                logger.info("✅ Fill: %s %s @ %s", row["symbol"], row["direction"], row["entry"])
            else:
                age = (datetime.datetime.now(datetime.timezone.utc) - row["opened_at"]).total_seconds() / 60
                if age > FILL_TIMEOUT_MIN:
                    # Verfallen. Der Close räumt eine etwaige Alt-Order in Cornix ab,
                    # damit sie nicht Tage später doch noch füllt und eine Position
                    # aufmacht, die niemand mehr überwacht.
                    close_mirror(conn, row, REASON_NOT_FILLED, None)
                else:
                    unfilled += 1
            continue

        state = TrailingState(
            entry=row["entry"],
            is_long=row["direction"] == "LONG",
            retrace_frac=RETRACE_FRAC,
            activation=ACTIVATION_PCT,
            peak_pct=row["peak_pct"] if row["peak_pct"] is not None else float("-inf"),
        )
        should_close, mark, peak_advanced = state.update(float(price))

        if should_close:
            close_mirror(conn, row, REASON_TRAIL, mark)
            continue

        # Zeit-Stop (T-052): NICHT scharf und älter als die Frist → zum Markt raus.
        # Strikt kausal — entschieden wird auf dem Peak-Stand von JETZT, nie darauf,
        # ob der Trade später noch scharf würde (genau dieser Look-ahead hat in der
        # Studie die Breakeven-Regeln um den Faktor 8 geschönt, Verdikt-Nachtrag 4).
        # Scharfe Spiegel gehören dem Trail: sein Stop liegt über +1,8 %, tiefe
        # Verlierer kann es dort nicht geben. `opened_at` statt `filled_at`, weil
        # Alt-Zeilen (vor T-050) kein filled_at tragen; seit dem Markt-Entry (T-051)
        # fallen beide ohnehin zusammen.
        if (
            TIME_STOP_H > 0
            and not state.armed
            and time_stopped < TIME_STOP_MAX_PER_CYCLE
            and row.get("opened_at") is not None
        ):
            age_h = (datetime.datetime.now(datetime.timezone.utc) - row["opened_at"]).total_seconds() / 3600
            if age_h > TIME_STOP_H:
                close_mirror(conn, row, REASON_TIME_STOP, mark)
                time_stopped += 1
                continue

        if peak_advanced:
            # Der Peak ist monoton — nur neue Hochs ändern dauerhaften Zustand. Das
            # hält die Schreibrate bei einer Handvoll pro Position statt einer pro
            # Poll pro Position, und es ist genau der Wert, ohne den ein Neustart
            # den Trail unterhalb eines längst gegebenen Peaks neu schärfen würde.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trailing_positions SET peak_pct = %s WHERE id = %s",
                    (state.peak_pct, row["id"]),
                )
            conn.commit()
            row["peak_pct"] = state.peak_pct

    if time_stopped:
        logger.info(
            "⏱ %d Spiegel per Zeit-Stop geschlossen (nie über +%.0f %% in %.0f h).",
            time_stopped,
            ACTIVATION_PCT,
            TIME_STOP_H,
        )
    if unfilled:
        logger.info("⏳ %d Spiegel warten noch auf ihren Fill (Cornix hat den Entry nicht erreicht).", unfilled)
    if missing:
        # Sichtbar machen, dass Positionen diesen Zyklus ohne Entscheidung blieben —
        # eine stille Lücke im Trailing wäre schlimmer als eine laute.
        logger.warning("⏸ %d Position(en) ohne Batch-Preis — Zyklus übersprungen, kein Einzelabruf.", missing)


def main() -> None:
    mode = "LIVE" if (LIVE_POSTING and TARGET_CHANNEL_ID) else "SHADOW"
    logger.info(f"=== 🪝 TRAILING CLOSE BOT STARTED ({mode}) ===")
    logger.info(
        f"Roster: 33 Beine aus {SOURCE_REPORT} · act={ACTIVATION_PCT}% · x={RETRACE_FRAC:.0%} · "
        f"cap={SLOT_CAP} (erwartet Ø {EXPECTED_OCC_MEAN:.0f} / p95 {EXPECTED_OCC_P95:.0f})"
    )
    if mode == "SHADOW":
        logger.warning(
            "SHADOW: TRAILING_BOT_LIVE_POSTING=1 UND CH_TRAILING sind nötig, um zu posten. "
            "Der Bot trackt und loggt, schreibt aber keine Outbox-Zeile."
        )

    # Optional, weil der Reconnect im except-Zweig unten fehlschlagen darf: dann
    # läuft die Schleife mit conn=None weiter und versucht es beim nächsten Poll
    # erneut, statt den Prozess zu verlieren. Der Rest der Schleife narrowt über
    # den `if conn is None`-Guard.
    conn: PooledConnection | None = get_db_connection()
    ensure_schema(conn)
    clear_unposted_carryover(conn)

    while True:
        try:
            time.sleep(POLL_SECONDS)
            if conn is None:
                conn = get_db_connection()
            conn.commit()  # frische Transaktions-Sicht, wie Monitor 8

            sources, all_open = read_source_signals(conn)
            mirrors = read_open_mirrors(conn)
            # Erst schliessen, dann eröffnen — in dieser Reihenfolge, damit ein im
            # selben Zyklus freigewordenes Symbol sofort neu belegt werden kann UND
            # das `Close <SYMBOL>` garantiert vor einem neuen Entry auf demselben
            # Symbol rausgeht: die Outbox ist per Channel strikt FIFO nach id
            # (4_telegram_bot.py, P0.1(d)/P1.3). Andersherum würde das Close den
            # frisch eröffneten Trade gleich wieder flach machen.
            # EIN Binance-Batch-Call pro Zyklus, geteilt von beiden Schritten.
            prices = get_live_prices_batch()
            poll_open_mirrors(conn, sources, mirrors, all_open, prices)
            open_mirrors(conn, sources, read_open_mirrors(conn), read_mirrored_src_ids(conn, set(sources)), prices)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Fehler im Trailing-Close-Bot: {e}", exc_info=True)
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            try:
                conn = get_db_connection()
            except Exception as reconnect_err:
                logger.error(f"Reconnect fehlgeschlagen: {reconnect_err}")
                conn = None
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Trailing Close Bot manuell gestoppt (Strg+C).")
