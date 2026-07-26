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

Invariants:
  * Schreibt NIE in ``ai_signals`` und schließt NIE einen Fremd-Trade — sein einziges
    Schreibrecht sind ``telegram_outbox`` (eigener Channel) und ``trailing_positions``.
  * Höchstens eine offene Spiegel-Position je Symbol (Cornix-Close ist symbol-weit).
  * Offene Spiegel-Positionen ≤ ``SLOT_CAP``.
  * Genau EINE Cornix-parsebare Nachricht je Entry (harte Regel 4).
  * Ein Bein ohne LIVE-Status in ``shadow_gate`` wird nie gespiegelt, auch wenn es
    im Roster steht.
"""

import datetime
import json
import logging
import os
import time

from core import config as _kcfg
from core import shadow_gate
from core.database import PooledConnection, get_db_connection
from core.live_price import get_live_price, get_live_prices_batch
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
# Trade ist? Der Spiegel übernimmt die Geometrie des Quell-Signals (Entry, SL, TPs),
# aber Cornix füllt zum AKTUELLEN Markt. Bei einem Trade von vor zwei Tagen misst
# der Trailing-Arm damit nicht mehr denselben Trade wie der Hold-Arm — und genau
# dieser Vergleich ist der Zweck des Bots.
#
# Der Wert deckt bewusst ein Restart-Fenster ab (der Fleet-Restart dauert ~5 min):
# Trades, die während des Neustarts aufgingen, will man noch mitnehmen, Altbestand
# nicht. Ohne diese Grenze spiegelt der Bot beim ersten Start den GESAMTEN offenen
# Bestand — im ersten Shadow-Lauf am 2026-07-26 waren das 465 Positionen auf einen
# Schlag, teils Tage alt. Dieselbe Klasse wie P2.7 im AI-Monitor ("kein
# Rückwirkend-Scoring von Alt-Trades nach Prozess-Neustart").
MAX_MIRROR_AGE_MIN = float(os.getenv("TRAILING_BOT_MAX_AGE_MIN", "15"))

# Exit-Gründe (landen in trailing_positions.close_reason)
REASON_TRAIL = "TRAIL"
REASON_SOURCE_CLOSED = "SOURCE_CLOSED"
REASON_LEG_RETIRED = "LEG_RETIRED"
#: Kein Exit, sondern ein Vermerk: dieser Quell-Trade lief schon, als der Bot
#: startete. Er wird nie gespiegelt — die Zeile existiert nur, damit er auch nie
#: wieder als Neuzugang erscheint (dieselbe Sperre wie ein geschlossener Spiegel).
REASON_PREEXISTING = "PREEXISTING"
#: Beim Umschalten von Shadow auf Live geschlossen: die Zeile war offen, aber nie
#: veröffentlicht, kann also keiner Position im Channel entsprechen.
REASON_SHADOW_CARRYOVER = "SHADOW_CARRYOVER"

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
                   EXTRACT(EPOCH FROM (NOW() - open_time)) / 60.0 AS age_min
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
    for sid, symbol, model, direction, entry1, price, sl, targets, lev, age_min in rows:
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
            "age_min": float(age_min) if age_min is not None else float("inf"),
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


def read_open_mirrors(conn) -> dict[int, dict]:
    """Eigene offene Spiegel-Positionen, ``src_signal_id`` → Zeile."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, src_signal_id, symbol, model, direction, entry, peak_pct, posted
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
        }
        for rid, src, symbol, model, direction, entry, peak, posted in rows
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


def close_messages(row: dict, reason: str, mark: float) -> tuple[str, str]:
    """(Close-Kommando, HTML-Info) für einen Exit.

    ``Close <SYMBOL>`` ist Cornix' Schließ-Kommando (``core/config.py:123``) und
    trifft ALLE Trades des Symbols im Channel — der Bot hält deshalb nie zwei
    Positionen auf einem Symbol. Das Kommando enthält keine Entry-Felder und ist
    damit nicht als neues Signal parsebar.
    """
    why = "trailing stop" if reason == REASON_TRAIL else "source trade closed"
    info = (
        "<pre>"
        + "\n".join(
            [
                f"<b>🔒 TRAILING CLOSE — {row['model']} {row['direction']}</b>",
                f"<b>{row['symbol']}</b>",
                f"<b>→ Reason: {why}</b>",
                f"<b>→ Mark: {mark:+.2f}% (unlevered)</b>",
            ]
        )
        + "</pre>"
    )
    return f"Close {row['symbol']}", info


# ─────────────────────────────────────────────────────────────────────────────
# ZULASSUNG
# ─────────────────────────────────────────────────────────────────────────────


def admit(candidates: list[tuple[int, dict]], held_symbols: set[str], free_slots: int) -> tuple[list, list]:
    """Wer darf in den Channel? Gibt ``(zugelassen, abgewiesen_mit_grund)`` zurück.

    Zwei Gründe, beide hart:
      * ``SYMBOL_HELD`` — auf dem Symbol läuft schon eine Spiegel-Position, und
        Cornix' Close ist symbol-weit.
      * ``SLOT_CAP`` — der Channel ist voll. Sortiert wird nach Bein-Dichte, damit
        bei Knappheit dasselbe Kriterium entscheidet, das die Auswahl überhaupt
        getroffen hat: Ertrag je belegtem Slot-Tag.

    Abweisungen werden zurückgegeben, nicht verschluckt — eine stille Deckelung
    liest sich später wie "alles gespiegelt".
    """
    admitted, rejected = [], []
    taken = set(held_symbols)
    for sid, sig in sorted(candidates, key=lambda c: -c[1]["density"]):
        if sig["symbol"] in taken:
            rejected.append((sid, sig, "SYMBOL_HELD"))
            continue
        if len(admitted) >= free_slots:
            rejected.append((sid, sig, "SLOT_CAP"))
            continue
        taken.add(sig["symbol"])
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
    oldest = max(sig["age_min"] for _sid, sig in stale)
    logger.info(
        "📎 %d Quell-Trade(s) als Altbestand vermerkt, nicht gespiegelt (ältester %.0f min, Grenze %.0f min).",
        len(stale),
        oldest,
        MAX_MIRROR_AGE_MIN,
    )


def open_mirrors(conn, sources: dict[int, dict], mirrors: dict[int, dict], already: set[int]) -> int:
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
    stale = [(sid, sig) for sid, sig in unseen if sig["age_min"] > MAX_MIRROR_AGE_MIN]
    if stale:
        record_preexisting(conn, stale)
    new = [(sid, sig) for sid, sig in unseen if sig["age_min"] <= MAX_MIRROR_AGE_MIN]
    if not new:
        return 0

    held = {m["symbol"] for m in mirrors.values()}
    admitted, rejected = admit(new, held, SLOT_CAP - len(mirrors))

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
    for sid, sig in admitted:
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
                    (src_signal_id, symbol, model, direction, entry, peak_pct, posted)
                VALUES (%s, %s, %s, %s, %s, NULL, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (sid, sig["symbol"], sig["model"], sig["direction"], sig["entry"], live),
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


def close_mirror(conn, row: dict, reason: str, mark: float) -> None:
    """Spiegel-Position schließen: Close-Kommando + eigene Zeile stempeln."""
    cmd, info = close_messages(row, reason, mark)
    if LIVE_POSTING and TARGET_CHANNEL_ID and row["posted"]:
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
    logger.info(f"🔒 Close {row['symbol']} ({row['model']} {row['direction']}) — {reason} @ {mark:+.2f}%")


def poll_open_mirrors(
    conn, sources: dict[int, dict], mirrors: dict[int, dict], all_open: set[int] | None = None
) -> None:
    """Preis-Poll über alle offenen Spiegel-Positionen (Trailing + Quell-Close)."""
    if not mirrors:
        return
    prices = get_live_prices_batch()
    open_ids = all_open if all_open is not None else set(sources)

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
            price = prices.get(row["symbol"]) or get_live_price(row["symbol"], conn)
            mark = 0.0
            if price:
                st = TrailingState(row["entry"], row["direction"] == "LONG", RETRACE_FRAC, ACTIVATION_PCT)
                mark = st.update(float(price))[1]
            close_mirror(conn, row, reason, mark)
            continue

        price = prices.get(row["symbol"])
        if price is None:
            price = get_live_price(row["symbol"], conn)
        if price is None:
            # Kein Preis heißt keine Entscheidung. Eine Position auf einem Coin ohne
            # Tick bleibt offen — sie zu schließen wäre eine Aussage über einen Markt,
            # den wir gerade nicht sehen.
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
            poll_open_mirrors(conn, sources, mirrors, all_open)
            open_mirrors(conn, sources, read_open_mirrors(conn), read_mirrored_src_ids(conn, set(sources)))

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
