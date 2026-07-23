# core/shadow_gate.py — fleet-weiter Shadow-Posting-Gate (T-2026-CU-9050-125).
#
# ZWECK: Jedes (model_tag, direction)-Bein, das NICHT live postet, soll statt
# Stille einen ÜBERWACHTEN Shadow-Trade erzeugen — damit unterdrückte Beine und
# noch-nicht-promotete Retrains eine realisierte Ergebnis-Historie
# (closed_ai_signals) für die spätere Auswertung aufbauen, inkl. der
# regime-konditionierten Freischaltung (Whitelist-v2-Flip, T-2026-CU-9050-069).
#
# WIE ES SICHER IST — "monitored but unposted": Ein Shadow-Trade ist eine
# ai_signals-Zeile OHNE telegram_outbox-Zeile. Der AI-Monitor (8_ai_trade_monitor)
# liest ai_signals ungefiltert, verfolgt Entry/TP/SL und schreibt beim Close eine
# closed_ai_signals-Zeile — er enthält KEINEN Posting-Code. Ein Kanal-Post
# passiert ausschließlich über eine telegram_outbox-Zeile. Kein Outbox-Insert =>
# nie ein Post (verifiziert T-2026-CU-9050-125). Details: docs/SHADOW_MODE_POSTING.md.
#
# SICHERHEITSVERTRAG (harte Regeln 1/2/4):
#   * DEFAULT = LIVE. Dieses Modul listet NUR Beine, die explizit SHADOW oder
#     RETIRED sind; alles andere ist live. Der Gate darf NIE einen bestehenden
#     Live-Post in einen Shadow-Post verwandeln — die Verdrahtung ist rein
#     ADDITIV am Nicht-Post-Zweig jedes Bots.
#   * Shadow-Trades tragen die Modell-Meta model_id (Regel 6). Live- und
#     Shadow-Beine desselben Modells trennt die `direction`; neue Generationen
#     tragen ohnehin einen neuen Tag (ATS2 vs. ATS1) → keine Kollision in
#     closed_ai_signals oder mit has_open_ai_signal.
#   * Master-Kill-Switch KYTHERA_SHADOW_POSTING=0 schaltet ALLE Shadow-Emission
#     ab (Bots fallen auf das heutige prediction-only-Verhalten zurück).

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Lifecycle-Zustände eines (tag, direction)-Beins.
LIVE = "live"  # postet live (Cornix + ai_signals) — Default
SHADOW = "shadow"  # erzeugt einen überwachten Shadow-Trade, kein Cornix-Post
RETIRED = "retired"  # alte Generation, wird nicht mehr emittiert (nur Historie)
SILENT = "silent"  # bewusst stummgeschaltetes Bein: KEIN Live-Post UND KEIN Shadow —
# das Modell läuft (der Bot ist entparkt), gibt aber nichts aus. Genutzt, um ein
# altes Bein (ATS1/ATB1) abzuschalten, während der Retrain (ATS2/ATB2) im Shadow
# datensammelt (Operator-Entscheid Michi, T-2026-CU-9050-127).

_DIRECTIONS = ("LONG", "SHORT")

# Verzeichnis der noch-nicht-promoteten Retrain-Artefakte (harte Regel 2:
# Modell-Artefakte leben in staging_models/, bis Michi sie in den Repo-Root
# promotet). Überschreibbar für Tests via KYTHERA_STAGING_DIR.
STAGING_DIR = os.environ.get("KYTHERA_STAGING_DIR", "staging_models")


def shadow_posting_enabled() -> bool:
    """Master-Schalter. Default AN; KYTHERA_SHADOW_POSTING=0 => komplett aus."""
    return os.environ.get("KYTHERA_SHADOW_POSTING", "1") == "1"


# ─────────────────────────────────────────────────────────────────────────────
# LIFECYCLE-REGISTER  —  (TAG_UPPER, DIRECTION) → Zustand
# ─────────────────────────────────────────────────────────────────────────────
# Nur NICHT-live Beine werden gelistet. Fehlt ein Bein hier, ist es LIVE.
# Quelle der Wahrheit ist die Kombination aus docs/MODEL_INTENT.md (Operator-
# Entscheide) UND dem tatsächlichen Gating im jeweiligen Bot. Jede Zeile trägt
# ihre Begründung — das ist der von Michi auditierbare Kern dieses Features.
#
# SHADOW-Beine zerfallen in zwei Klassen:
#   (A) Neue Generation, noch nicht promotet: das Modell existiert nur in
#       staging_models/ und läuft PARALLEL zum weiter-live alten Tag
#       (ATS2 neben ATS1, ATB2 neben ATB1, …). Siehe SHADOW_ARTIFACTS.
#   (B) Unterdrücktes Richtungs-Bein eines sonst-live Modells: das Modell ist
#       geladen, aber die Richtung geht (noch) nicht live.
_LIFECYCLE: dict[tuple[str, str], str] = {
    # ── (A) Neue-Generation-Shadow-Kandidaten (staging, nicht promotet) ──
    # ATS2 (Bot 12): am 2026-07-23 SHADOW→LIVE promotet (T-2026-KYT-9050-033, Audit
    # T-032 — ATS2-Shadow schlägt das stummgeschaltete ATS1 in beiden Richtungen:
    # LONG +0.31%×612, SHORT dünn). BEIDE Beine daher NICHT mehr gelistet ⇒ Default
    # LIVE. Bot 12 postet ATS2 jetzt über post_ai_signal_gated (T-033-Rewire von
    # _emit_ats2_shadow), sodass der LIVE-Zustand echten Cornix-Post erzeugt.
    #   DEPLOY-VORBEDINGUNG (Michi, harte Regel 2): ats2_model_{LONG,SHORT}.pkl aus
    #   staging_models/ nach Repo-Root promoten — der LIVE-Loader liest den Root-Pfad
    #   (shadow_artifact_path), sonst lädt ATS2 nichts und schweigt. Thresholds sind
    #   real (LONG 0.7825 / SHORT 0.9084), also KEIN Flood-Risiko.
    # ATB2: Converging-Channel-Neuaufbau (Bot 14). ATB1 ist stummgeschaltet (C); ATB2 hat
    # optimal_threshold=null (LONG) bzw. ist nicht deploybar (SHORT) → braucht
    # zwingend Shadow-Datensammlung, bevor je ein Operating-Point wählbar ist.
    ("ATB2", "LONG"): SHADOW,
    ("ATB2", "SHORT"): SHADOW,
    # SRA2: Meta-Filter-Retrain (Bot 9). SRA1 bleibt live. SRA2 war "nicht
    # deploybar", WEIL die Label-Quelle closed_trades3 seit Feb tot ist — ein
    # reines TRAININGS-Problem. Shadow-Serving umgeht das komplett: der AI-Monitor
    # liefert die frischen Outcomes (closed_ai_signals), die der tote Tracker nicht
    # mehr gibt → Shadow REVIVED SRA2. SHORT-Threshold ist null (jedes Setup).
    # SRA2 LONG am 2026-07-21 LIVE promotet (T-2026-CU-9050-185, @0.6424 → CH_AI_SR,
    # koexistierend mit SRA1). Artefakt sra2_model_LONG.* nach Repo-Root promotet
    # (Regel 2, Operator-Entscheid Michi).
    # SRA2 SHORT am 2026-07-23 SHADOW→LIVE promotet (T-2026-KYT-9050-033, Audit T-032:
    # SRA2-SHORT-Shadow +1.00%×222). NICHT mehr gelistet ⇒ Default LIVE; Bot 9 postet
    # SRA2 SHORT bereits über post_ai_signal_gated (kein Rewire nötig).
    #   DEPLOY-VORBEDINGUNGEN (Michi): (1) sra2_model_SHORT.json aus staging_models/
    #   nach Repo-Root (Regel 2); (2) ⚠ optimal_threshold ist NULL (Meta: deployable
    #   false, val avg_net_pnl −0.079%) → LIVE würde auf JEDEM S/R-SHORT-Kandidaten
    #   posten (Cornix-Flood, kein Prob-Gate). Vor Go-Live einen Threshold setzen bzw.
    #   retrainen — sonst ist die Promotion ein Flood-Risiko (im Report geflaggt).
    # MAX2 (T-2026-KYT-9050-020): KEIN Modell — ein Fork der SRA2-LONG-Emission in
    # Bot 9 (_emit_max2), der denselben SRA2-LONG-Trade coin-gefiltert (config.
    # MAIN_CHANNEL_COINS) nach CH_MAIN postet und den retireten klassischen
    # "Main Channel"-Detektor ersetzt. Bewusst NICHT gelistet ⇒ default LIVE
    # (Operator-Entscheid Michi): kollisionsfrei mit dem SRA2-Post nach CH_AI_SR,
    # weil CH_AI_SR NICHT Cornix-executed ist (kein Regel-4-Doppel-Trade). Nur
    # LONG (SRA2 SHORT tot). Rollback in den Shadow = die folgende Zeile
    # einkommentieren (kein Cornix mehr, nur überwachter Shadow-Trade):
    # ("MAX2", "LONG"): SHADOW,
    # FMR2: Normalisierungs-Exit-Retrain (K4, T-2026-CU-9050-148) neben dem FMR1-Bot
    # (Bot 31). FMR1 bleibt unverändert unter eigenem Tag "FMR1"; FMR2 nutzt DENSELBEN
    # Funding-Extrem-Detektor + `build_fmr1_row`-Feature-Row (FMR2_FEATURES ==
    # FMR1_FEATURES, nur das Label unterscheidet sich) → getreue Parität. Der Retrain
    # war nicht deploybar (beide Richtungen netto-negativ, AUC ~0,54) → Shadow zur
    # Live-Gegenprüfung; ein Modell für beide Richtungen (side_short ist Feature),
    # optimal_threshold 0,46. KEINE Tag-Kollision (FMR1 postet unter "FMR1").
    ("FMR2", "LONG"): SHADOW,
    ("FMR2", "SHORT"): SHADOW,
    # ── (B) Challenger-Beine: der Retrain fordert ein LIVE-Bein heraus, das bereits
    #        unter DEMSELBEN Tag postet → eigener Generations-Tag, sonst würde der
    #        Shadow-Trade über den Active-Trade-Check des Bots einen LIVE-Post
    #        blockieren (Verletzung der rein-additiven Invariante). ──
    # RUB3 = rub2_model_LONG-Retrain vs. LIVE-RUB-LONG (Bot 13 postet Legacy unter
    # "RUB2"). Operator-Entscheid Michi (Regel 6). SHORT bleibt live "RUB2".
    ("RUB3", "LONG"): SHADOW,
    # RUB4 (T-2026-CU-9050-164): funding-gegatetes RUB-LONG — DERSELBE RUB3-
    # Kandidat, aber nur wenn fund_24h > +3 bps (ABR1-LONG-Gate). Experiment, ob
    # das Gate das blutende RUB-LONG rettet; RUB4 vs. RUB3 = gegatet vs. ungegatet
    # im Report. Nutzt das RUB3-Artefakt (kein eigener SHADOW_ARTIFACTS-Eintrag).
    ("RUB4", "LONG"): SHADOW,
    # EPD3 = epd2_model_{LONG,SHORT}-Retrain vs. LIVE-EPD (Bot 10 postet das Legacy-
    # Modell bereits unter Tag "EPD2" = EPD_LEGACY_TAG; ein Shadow unter "EPD2"
    # würde über den dortigen Active-Trade-Check `model IN ('EPD2','EPD2')` einen
    # Live-Post unterdrücken). Deshalb eigener Tag "EPD3" — analog zu RUB3.
    # EPD3 SHORT war am 2026-07-21 LIVE promotet (T-2026-CU-9050-185, @0.6737 →
    # CH_PUMP_AI). Am 2026-07-23 LIVE→SHADOW GEPARKT (T-2026-KYT-9050-033, Audit T-032:
    # EPD3-SHORT [act] net −0.06%×3568 — Edge weg). Bot 10 routet EPD3 über
    # post_ai_signal_gated ⇒ reiner Register-Flip. LONG bleibt SHADOW (threshold=None).
    #   ⚠ DEPLOY-HINWEIS (Michi): das Live-Artefakt liegt als epd3_model_SHORT.pkl im
    #   Repo-ROOT; als SHADOW-Bein liest shadow_artifact_path staging_models/
    #   epd3_model_SHORT.pkl — die Datei fehlt dort. Ohne sie lädt EPD3 SHORT nicht und
    #   wird effektiv STILL (statt shadow-getrackt). Für echte Shadow-Historie das
    #   Artefakt nach staging_models/ kopieren; sonst ist der Park schlicht Silence (ok).
    ("EPD3", "LONG"): SHADOW,
    ("EPD3", "SHORT"): SHADOW,
    # ── (C) Stummgeschaltete Alt-Beine (Operator Michi, T-2026-CU-9050-127) ──
    # Bots 12/14 werden entparkt, damit ATS2/ATB2 im Shadow laufen — aber die
    # ALTEN Modelle ATS1/ATB1 sollen NICHT live posten und auch nicht shadowen:
    # sie gehen komplett still. Der Bot fragt is_live() am Post-Zweig; SILENT ⇒
    # nicht live ⇒ der ganze ATS1/ATB1-Ausgabe-Zweig wird übersprungen.
    ("ATS1", "LONG"): SILENT,
    ("ATS1", "SHORT"): SILENT,
    ("ATB1", "LONG"): SILENT,
    ("ATB1", "SHORT"): SILENT,
    # FIF1: von TSM1 (SHORT → CH_FIF1) abgelöst (T-2026-CU-9050-183). Am 2026-07-23
    # SILENT→SHADOW revived (T-2026-KYT-9050-033, Audit T-032: FIF1 weiter als
    # überwachter Shadow beobachten statt ganz still). Bot 33 hatte nur einen
    # LIVE-oder-nichts-Zweig; T-033 ergänzt einen SHADOW-Zweig (post_shadow_ai_signal),
    # sonst erzeugt die Revive keine monitored Trades. TSM1 bleibt der Live-Nachfolger
    # auf CH_FIF1 (Block (D)); der FIF1-Shadow kollidiert nicht (eigener Tag, kein
    # Cornix). Vollständiges Silence wieder = diese zwei Zeilen auf SILENT setzen.
    ("FIF1", "LONG"): SHADOW,
    ("FIF1", "SHORT"): SHADOW,
    # ── (D) Regelbasierte Shadow-Forwarder (T-2026-CU-9050-149) ──
    # Studien-Kandidaten K1/K2/K5/K7 sind REGELN, kein Modell — kein Artefakt in
    # SHADOW_ARTIFACTS. Der Bot rechnet das Signal selbst und emittiert auf dem
    # ROH-Signal (ROM1-Präzedenz), gegated NUR über diese SHADOW-Zeile. Alle
    # Backtests negativ/schwach → Shadow = Live-Gegenprüfung, kein Rollout.
    # LIS1 (K5): Post-Listing-Drift-Fade, nur SHORT (LONG-Blacklist ist ein
    # separates Gate, Operator-Sache). Bot 36 postet NIE live (fail-safe: ist das
    # Bein nicht SHADOW, schweigt der Bot — die Regel hat keinen Edge).
    ("LIS1", "SHORT"): SHADOW,
    # TSM1 (K1, SHORT), SKW1 (K7, LONG+SHORT), XSM1 (K2, LONG) und XSR1 (K2,
    # SHORT) wurden am 2026-07-20 LIVE promotet (T-2026-CU-9050-183, Operator-
    # Entscheid Michi aus dem 14:00-Report-Review) — sie stehen daher NICHT mehr
    # hier (Default LIVE). Routing: TSM1 SHORT → CH_FIF1 (ersetzt FIF1, s. Block
    # (C)); SKW1 LONG+SHORT + XSM1 LONG + XSR1 SHORT → CH_ATS (ehem. ATS-Channel).
    # Der Live-Post läuft über signal_post.post_ai_signal_gated in Bot 37/38/39;
    # ein Rückzug in den Shadow = die jeweilige (tag, dir)-Zeile hier wieder mit
    # SHADOW eintragen. LIS1 SHORT bleibt shadow-only (weiter falsifiziert).
    #
    # ══════════════════════════════════════════════════════════════════════════
    # (E) Fleet-Reconfig nach Audit T-032 (T-2026-KYT-9050-033, Operator-Entscheid
    #     Michi aus bot_results.xlsx). Alle Beine hier bluten realisiert und werden
    #     geparkt (Cornix aus, monitored Shadow an) bzw. beide Beine stummgeschaltet.
    #     Diese Tags posten NICHT über post_ai_signal_gated, sondern legacy-direkt;
    #     ihre Bots konsultieren den Gate seit T-033 an der Emissions-Stelle über
    #     core.signal_post.route_legacy_leg — der Register-Eintrag wird damit wirksam.
    #     Keys sind UPPER-normalisiert (leg_status _norm()t den Lookup, nicht den Key).
    # ── Park SHORT →SHADOW (LONG bleibt LIVE), Audit: SHORT-Bein netto-negativ ──
    # BR (Bot 7, Pattern-Breakout): 2h/4h SHORT geparkt; LONG live. BR1H ist der
    # PRE-Rename-Tag (Bot 7 postet 1h heute als BR1Hv2 = BR1HV2, s. (E)-Ganz-Block) —
    # die BR1H-Zeile ist dokumentarisch (kein aktiver Emitter mehr).
    ("BR1H", "SHORT"): SHADOW,
    ("BR2H", "SHORT"): SHADOW,
    ("BR4H", "SHORT"): SHADOW,
    # BB (Bot 25, SMC-Sniper): 1h/4h SHORT geparkt; LONG live. TD (gleicher Bot)
    # bleibt komplett LIVE (Audit KEEP) → keine TD-Zeile.
    ("BB_1H", "SHORT"): SHADOW,
    ("BB_4H", "SHORT"): SHADOW,
    # QM (Bot 24, Quasimodo): 1h SHORT geparkt; LONG live. QM_4H fährt der Bot ohnehin
    # nicht mehr (TIMEFRAMES=['1h']) → die QM_4H-Zeile ist dokumentarisch.
    ("QM_1H", "SHORT"): SHADOW,
    ("QM_4H", "SHORT"): SHADOW,
    # ── Park LONG →SHADOW (SHORT bleibt LIVE), Audit: LONG-Bein netto-negativ ──
    # MIS2 (Bot 11): 24h/72h/168h LONG geparkt (Pump-Seite bleibt hinter der besser
    # realisierenden SHORT/Dump-Seite zurück); SHORT live. MIS2-8H steht im Ganz-Block.
    ("MIS2-24H", "LONG"): SHADOW,
    ("MIS2-72H", "LONG"): SHADOW,
    ("MIS2-168H", "LONG"): SHADOW,
    # EPD1: PRE-Rename-Tag (Bot 10 postet heute EPD2 = EPD_LEGACY_TAG). Kein aktiver
    # EPD1-Emitter mehr → dokumentarisch; der Park wirkt über den EPD2-Ganz-Block.
    ("EPD1", "LONG"): SHADOW,
    # ── Ganz →SHADOW (beide Beine), Audit: beide Richtungen netto-negativ ──
    # EPD2 (Bot 10, Legacy-Pump/Dump-Direktpost): beide Beine geparkt.
    ("EPD2", "LONG"): SHADOW,
    ("EPD2", "SHORT"): SHADOW,
    # MIS2-8H (Bot 11): beide Beine geparkt (8h-Horizont laut Studie ohnehin negativ).
    ("MIS2-8H", "LONG"): SHADOW,
    ("MIS2-8H", "SHORT"): SHADOW,
    # ── (E-MIS1) MIS1-Revive (T-2026-KYT-9050-034, Operator-Entscheid Michi) ──
    # Bot 11 belebt die MIS1-Generation (pump_model_*_final.pkl) PARALLEL zu MIS2
    # unter eigenen Tags MIS1-* wieder (Audit T-032: MIS1 realisierte besser). Die
    # GUTEN Beine sind Default-LIVE (NICHT gelistet) und beleben genau die von T-033
    # geparkten MIS2-Beine: MIS1-24H/72H/168H LONG (Pump) + MIS1-8H SHORT (Dump).
    # Die SCHWACHEN MIS1-Beine werden hier auf SHADOW geparkt (überwachter Trade,
    # kein Cornix) — MIS1-8H LONG (8h-Pump negativ) + MIS1-24H/72H/168H SHORT (dort
    # ist die MIS2-SHORT/Dump-Seite die live-gehaltene Generation, T-033). Kein
    # Cornix-Doppel-Post je Leg: pro (Horizont, Richtung) ist genau EINE Generation
    # live. Keys UPPER (leg_status _norm()t den Lookup).
    ("MIS1-8H", "LONG"): SHADOW,
    ("MIS1-24H", "SHORT"): SHADOW,
    ("MIS1-72H", "SHORT"): SHADOW,
    ("MIS1-168H", "SHORT"): SHADOW,
    # RUB2 (Bot 13, Rubberband — Legacy-LONG + RUB2_SHORT-Modell, beide Direktpost):
    # beide Beine geparkt. RUB3/RUB4 (LONG-Challenger) bleiben unverändert Shadow (oben).
    ("RUB2", "LONG"): SHADOW,
    ("RUB2", "SHORT"): SHADOW,
    # SRA1 (Bot 9, S/R-Legacy-Direktpost): beide Beine geparkt. SRA2 ist der Nachfolger
    # (LONG + SHORT jetzt LIVE, s. (A)) → SRA1 tritt ab in den Shadow.
    ("SRA1", "LONG"): SHADOW,
    ("SRA1", "SHORT"): SHADOW,
    # BB2_4H (Bot 25, BB-Retrain-Generation): beide Beine geparkt (Audit RETIRE beidseitig).
    ("BB2_4H", "LONG"): SHADOW,
    ("BB2_4H", "SHORT"): SHADOW,
    # BR1D + BR1Hv2 (Bot 7): beide Beine geparkt (1d + der aktuelle 1h-Tag BR1HV2).
    ("BR1D", "LONG"): SHADOW,
    ("BR1D", "SHORT"): SHADOW,
    ("BR1HV2", "LONG"): SHADOW,
    ("BR1HV2", "SHORT"): SHADOW,
    # ABR2 (Bot 18, Break&Retest Gen-2 — SHORT-Binärmodell + LONG-Funding-Gate, beide
    # posten als ABR2): beide Beine geparkt. ABR1 (Legacy-Fallback-Tag) bleibt Default
    # LIVE, wird vom Bot aber nur bei fehlender Binär-Meta emittiert (heute nicht).
    ("ABR2", "LONG"): SHADOW,
    ("ABR2", "SHORT"): SHADOW,
    # "Main Channel" (klassisch): bereits retired via Detektor-Dispatch-Removal
    # (T-2026-KYT-9050-020, ersetzt durch MAX2) → kein Emitter, kein Eintrag nötig.
}

# RETIRED: Tags, die in der closed_ai_signals-Historie vorkommen, aber von keinem
# Live-Bot mehr emittiert werden. Reine Report-Klassifikation (Teil 2) — kein
# Posting-Effekt. Richtung ist hier egal (beide Richtungen retired).
_RETIRED_TAGS: set[str] = {
    "AIM1",  # §9: AIM1-Konzept offiziell abgelöst durch AIM2 (Ranker/Gate).
    # MIS1 (T-2026-KYT-9050-034): NICHT mehr retired — die MIS1-Generation ist
    # REVIVED (Bot 11 lädt pump_model_*_final.pkl wieder, Operator-Entscheid Michi;
    # Audit T-032: MIS1 realisierte besser als MIS2). Lifecycle je (tag, direction)
    # steuert jetzt der _LIFECYCLE-Block (E-MIS1) unten: die guten Beine
    # (MIS1-24H/72H/168H LONG + MIS1-8H SHORT) sind Default-LIVE, die schwachen
    # dort auf SHADOW geparkt.
    "MSI1",  # historischer MIS-Typo-Family-Tag (bot_naming normalisiert → MIS1).
}


# ─────────────────────────────────────────────────────────────────────────────
# SHADOW-ARTEFAKTE  —  Klasse-(A)-Modelle aus staging_models/
# ─────────────────────────────────────────────────────────────────────────────
# Pro neuem Tag die Artefakt-Dateinamen je Richtung. Der Bot lädt sie über
# load_shadow_artifact() zusätzlich zu seinem Live-Modell und scored parallel.
# Fehlt die Datei (nicht gestaget), liefert der Loader None → der Bot läuft
# unverändert weiter (kein harter Fehler).
SHADOW_ARTIFACTS: dict[str, dict[str, str]] = {
    "ATS2": {"LONG": "ats2_model_LONG.pkl", "SHORT": "ats2_model_SHORT.pkl"},
    "ATB2": {"LONG": "atb2_model_LONG.pkl", "SHORT": "atb2_model_SHORT.pkl"},
    "SRA2": {"LONG": "sra2_model_LONG.json", "SHORT": "sra2_model_SHORT.json"},
    # Challenger-Tags (siehe _LIFECYCLE (B)) — Artefakt-Dateiname trägt weiter die
    # Retrain-Generation, der Tag darüber ist der kollisionsfreie Shadow-Tag.
    "RUB3": {"LONG": "rub2_model_LONG.pkl"},
    # EPD3 SHORT ist LIVE promotet (T-2026-CU-9050-185) → challenger-DISTINKTER
    # Root-Dateiname epd3_model_SHORT.pkl, damit die Promotion NICHT den Legacy-
    # EPD2-Loader-Slot kapert (Bot 10: EPD2_ARTIFACT_PATHS["SHORT"]=
    # "epd2_model_SHORT.pkl") — sonst lädt der EPD2-Live-Pfad dieselbe Datei und
    # postet SHORT doppelt (Tag EPD2 + EPD3, Regel-4-Doppel-Trade; Review T-185).
    # LONG bleibt Shadow aus staging unter dem epd2-Namen (kollidiert nicht: der
    # Legacy liest ROOT, der Shadow staging).
    "EPD3": {"LONG": "epd2_model_LONG.pkl", "SHORT": "epd3_model_SHORT.pkl"},
    # FMR2: ein binäres Modell für BEIDE Richtungen (side_short ist ein Feature) →
    # dieselbe Datei je Richtung; nicht promotet, nur Shadow (T-2026-CU-9050-148/149).
    "FMR2": {"LONG": "fmr2_model.pkl", "SHORT": "fmr2_model.pkl"},
}


def _norm(tag: str) -> str:
    return (tag or "").strip().upper()


def is_retired(tag: str, direction: str = "") -> bool:
    """True, wenn der Tag zu einer abgelösten Generation gehört. Prefix-Grenze,
    weil closed_ai_signals-Tags Familien sind (``MIS1-8h``, ``MIS1-72H``) — aber
    ``MIS2-8h`` darf NICHT auf ``MIS1`` matchen."""
    t = _norm(tag)
    for rt in _RETIRED_TAGS:
        if t == rt or t.startswith(rt + "-") or t.startswith(rt + "_"):
            return True
    return False


def leg_status(tag: str, direction: str) -> str:
    """Lifecycle-Zustand eines Beins. Default LIVE (Sicherheitsvertrag)."""
    if is_retired(tag):
        return RETIRED
    return _LIFECYCLE.get((_norm(tag), _norm(direction)), LIVE)


def is_live(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == LIVE


def is_shadow(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == SHADOW


def is_silent(tag: str, direction: str) -> bool:
    return leg_status(tag, direction) == SILENT


def shadow_artifact_path(tag: str, direction: str) -> str | None:
    """Pfad des Artefakts eines Klasse-(A)/Challenger-Tags, oder None ohne Eintrag.

    Regel-2-Promotion (T-2026-CU-9050-185): ein LIVE-Bein lädt sein Artefakt aus
    dem Repo-ROOT (dorthin promotet = live, Operator-Entscheid), ein SHADOW-Bein
    weiter aus ``staging_models/``. So kann ein einzelnes Richtungs-Bein eines
    Tags live gehen (z. B. SRA2 LONG @ Root), während das andere Shadow bleibt
    (SRA2 SHORT im Staging) — der Loader greift automatisch die richtige Datei."""
    fname = SHADOW_ARTIFACTS.get(_norm(tag), {}).get(_norm(direction))
    if not fname:
        return None
    if is_live(tag, direction):
        return fname  # promotet nach Repo-Root
    return os.path.join(STAGING_DIR, fname)


def load_shadow_artifact(tag: str, direction: str):
    """Lädt ein Klasse-(A)-Shadow-Modell aus staging_models/ (fail-soft).

    Normalisiert BEIDE Fleet-Artefakt-Formate auf ein schlankes Shadow-Contract-
    Dict ``{model, features, threshold}``:
      * ``.pkl`` — retrain_from_replay joblib-dict (ats2/atb2/rub2/epd2/max1),
        Keys ``model / features / optimal_threshold``.
      * ``.json`` — natives XGB-JSON + ``_meta.json``-Sidecar (sra2/abr2).

    Wichtig: Die PRODUKTIONS-Loader (core.model_artifacts) verweigern hier —
    ``build_contract`` macht ``float(optimal_threshold)`` und CRASHT auf den
    NICHT-deploybaren Retrains (threshold ``null`` bei ATB2/SRA2-SHORT/EPD2-LONG/
    RUB2-LONG). Genau die wollen wir aber shadow-sammeln, deshalb dieser tolerante
    Loader: ``threshold=None`` ist zulässig (→ Emission auf JEDEM Kandidaten).

    Rückgabe: ``{model, features, threshold}`` oder None (Tag unbekannt / Datei
    fehlt / Ladefehler — Bot läuft dann ohne Shadow-Bein weiter, harte Regel 2).
    """
    path = shadow_artifact_path(tag, direction)
    if not path or not os.path.exists(path):
        return None
    try:
        if path.endswith(".json"):
            import json

            import xgboost as xgb

            model = xgb.XGBClassifier()
            model.load_model(path)
            meta_path = path[:-5] + "_meta.json"
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            return {"model": model, "features": list(meta["features"]), "threshold": meta.get("optimal_threshold")}
        import joblib

        art = joblib.load(path)
        return {"model": art["model"], "features": list(art["features"]), "threshold": art.get("optimal_threshold")}
    except Exception as e:  # pragma: no cover - defensiv, Bot darf nicht sterben
        logger.warning("Shadow-Artefakt %s/%s laden fehlgeschlagen (%s): %s", tag, direction, path, e)
        return None


def artifact_threshold(artifact) -> float | None:
    """Operating-Threshold aus dem Contract-Artefakt (``optimal_threshold``).

    None bedeutet: das Modell hat KEINEN validen Operating-Point (z. B. ATB2 —
    zu dünne Daten, pick_threshold_safe hat verweigert). Der Bot emittiert dann
    auf JEDEM Detektor-Event (der Detektor ist das Gate), damit überhaupt
    Shadow-Daten für eine spätere Threshold-Wahl entstehen. Ist ein Threshold
    gesetzt, emittiert der Bot nur bei prob >= threshold — getreue Vorschau des
    Live-Verhaltens nach einer Promotion.
    """
    if not isinstance(artifact, dict):
        return None
    # Normalisierter Shadow-Contract nutzt "threshold"; roher joblib-dict (falls
    # ein Bot direkt joblib.load nutzt) trägt "optimal_threshold".
    thr = artifact.get("threshold", artifact.get("optimal_threshold"))
    try:
        return float(thr) if thr is not None else None
    except (TypeError, ValueError):
        return None


def score_artifact(artifact, feature_row: dict) -> float:
    """ROHE ``predict_proba[:, 1]`` des Contract-Artefakts auf einem Feature-Dict.

    Gate-Semantik ist ROH: ``pick_threshold_safe`` (tools/retrain_from_replay.py)
    wählt ``optimal_threshold`` auf der rohen predict_proba; der mitgelieferte
    Isotonic-Kalibrator ist nur Reporting (identisch zu Bot 13/25). Der Feature-
    Vertrag (Reihenfolge + Auswahl) kommt aus ``artifact["features"]``.
    """
    import pandas as pd

    feats = artifact["features"]
    X = pd.DataFrame([feature_row]).reindex(columns=feats).fillna(0)
    return float(artifact["model"].predict_proba(X)[0, 1])
