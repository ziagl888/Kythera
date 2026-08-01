#!/usr/bin/env python3
# tools/promotion_guard.py — Challenger-Promotion-Namensguard (T-2026-KYT-9050-057).
#
# DAS PROBLEM (Review-Finding aus T-185 / PR #170, seither latent):
# `core.shadow_gate.shadow_artifact_path` gibt für ein SHADOW-Bein den Staging-
# Pfad zurück, für ein LIVE-Bein aber den NACKTEN Root-Dateinamen — die Promotion
# ist damit allein durch den Register-Flip (SHADOW→LIVE) + das Kopieren der Datei
# in den Repo-Root definiert. `SHADOW_ARTIFACTS` trägt für Challenger-Tags
# historisch den Dateinamen der Retrain-GENERATION, nicht den des Tags:
#
#     "RUB3": {"LONG": "rub2_model_LONG.pkl"}   # ← Tag RUB3, Datei rub2_*
#
# Geht so ein Bein live, liest der Challenger den Root-Slot einer FREMDEN
# Generation — und, schlimmer, die Promotion legt die Challenger-Datei in genau
# den Slot, aus dem der Legacy-Bot sein Live-Modell lädt. Bei EPD3-SHORT war das
# 2026-07-21 real (`epd2_model_SHORT.pkl` = Bot-10-`EPD2_ARTIFACT_PATHS["SHORT"]`):
# der EPD2-Live-Pfad hätte dasselbe Modell geladen und unter zwei Tags gepostet
# (Regel-4-Doppel-Trade, echtes Geld). Gefixt wurde das damals VON HAND, indem das
# Root-Artefakt challenger-distinkt `epd3_model_SHORT.pkl` genannt wurde; bei
# EPD3-LONG (T-037) dieselbe Handarbeit ein zweites Mal.
#
# WAS DIESER GUARD TUT: er automatisiert genau diese Handarbeit — er prüft für
# jedes Bein in `SHADOW_ARTIFACTS`, ob sein Promotions-Ziel (der Root-Dateiname)
# challenger-distinkt ist, und schlägt sonst den tag-eigenen Namen vor. Ein
# LIVE-Bein mit fremdem Slot ist ein FAIL (exit 1), ein noch-SHADOW-Bein ein WARN
# (latenter Promotions-Blocker). Heute: RUB3-LONG ist WARN, kein FAIL — RUB3 ist
# per T-2026-KYT-9050-037 auf SHADOW geparkt, der Hazard also scharf, aber nicht
# ausgelöst. Der Flip auf LIVE ohne Rename dreht ihn auf FAIL.
#
# Invariants:
#   * READ-ONLY und rein registry-basiert: kein DB-Zugriff, kein Netzwerk, kein
#     Dateisystem-Scan, keine Promotion (harte Regeln 1/2). Damit ist das Urteil
#     unabhängig davon, welche Artefakte auf DIESER Maschine liegen.
#   * KEINE Verhaltensänderung an `core.shadow_gate` (harte Regel 7: der Gate wird
#     von Bots UND Trainer/Replay importiert). Der Guard LIEST das Register.
#   * Die Tag→Root-Dateiname-Brücke der Legacy-/Live-Generationen kommt aus
#     `tools.bot_variants.index.legacy_artifact_slots()` — eine bereits getestete
#     Quelle statt eines zweiten kuratierten Dicts.
#
# Aufruf:
#   python tools/promotion_guard.py            # Findings + exit 1 bei FAIL
#   python tools/promotion_guard.py --strict   # WARNs zählen auch als exit 1

from __future__ import annotations

import argparse
import os
import sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.shadow_gate as shadow_gate  # noqa: E402
import tools.bot_variants.index as variant_index  # noqa: E402

OK = "PASS"
WARN = "WARN"
FAIL = "FAIL"


def _norm(tag: str) -> str:
    """Tag-Normalisierung wie im Register (Keys sind dort UPPER)."""
    return (tag or "").strip().upper()


class Finding(NamedTuple):
    """Ein Befund je (Tag, Richtung). ``severity`` ist WARN oder FAIL."""

    tag: str
    direction: str
    filename: str
    severity: str
    reasons: tuple[str, ...]
    suggestion: str

    def as_line(self) -> str:
        return (
            f"{self.severity} {self.tag}/{self.direction}: '{self.filename}' — "
            + "; ".join(self.reasons)
            + f" → challenger-distinkt benennen: '{self.suggestion}'"
        )


def slot_claims() -> dict[str, set[str]]:
    """Root-Dateiname → Menge der Tags, die diesen Loader-Slot beanspruchen.

    Vereinigung aus (a) der kuratierten Legacy-/Live-Registry (die Root-Slots, aus
    denen die Fleet-Bots ihre Modelle laden) und (b) `SHADOW_ARTIFACTS` (die
    Klasse-(A)/Challenger-Tags). Ein Dateiname mit >1 Tag ist der Kollisions-
    Hazard: bei der Promotion landet EIN Artefakt in einem Slot, den ZWEI
    Generationen lesen."""
    claims: dict[str, set[str]] = {}
    for tag, dirmap in variant_index.legacy_artifact_slots().items():
        for filenames in dirmap.values():
            for filename in filenames:
                claims.setdefault(filename, set()).add(tag.upper())
    for tag, dirmap in shadow_gate.SHADOW_ARTIFACTS.items():
        for filename in dirmap.values():
            claims.setdefault(filename, set()).add(tag.upper())
    return claims


def tag_prefix(tag: str) -> str:
    """Der Dateinamen-Präfix, den ein tag-eigenes Artefakt tragen muss.

    Konvention der bereits gefixten Fälle: Tag `EPD3` → `epd3_model_SHORT.pkl`.
    Bindestriche der Horizont-Tags (`MIS2-8H`) fallen weg, damit der Präfix ein
    gültiger Dateinamen-Token bleibt."""
    return _norm(tag).replace("-", "").lower()


def suggested_name(tag: str, filename: str) -> str:
    """Challenger-distinkter Vorschlag: führenden Generations-Token ersetzen.

    'rub2_model_LONG.pkl' + RUB3 → 'rub3_model_LONG.pkl'. Trägt der Name keinen
    '_'-Token (Legacy-Einzeldateien), wird der Präfix vorangestellt."""
    prefix = tag_prefix(tag)
    head, sep, tail = filename.partition("_")
    if sep and head.lower() != prefix:
        return f"{prefix}_{tail}"
    if sep:
        return filename
    return f"{prefix}_{filename}"


def check_leg(tag: str, direction: str, claims: dict[str, set[str]] | None = None) -> Finding | None:
    """Prüft EIN (Tag, Richtung)-Bein aus `SHADOW_ARTIFACTS`. None = sauber.

    Zwei unabhängige Belege für "nicht challenger-distinkt":
      (1) OWNERSHIP — der Dateiname wird von mindestens einem FREMDEN Tag
          beansprucht (der harte Beleg: da liest wirklich ein anderer Loader).
      (2) KONVENTION — der Dateiname trägt nicht den tag-eigenen Präfix. Fängt
          auch den Fall, in dem der fremde Loader (noch) in keiner Registry steht.

    Die Schwere kommt aus dem Lifecycle: ein LIVE-Bein liest den fremden Root-Slot
    JETZT (FAIL), ein SHADOW/SILENT/RETIRED-Bein erst nach der nächsten Promotion
    (WARN — latenter Blocker, den der Operator vor dem Flip sehen muss)."""
    filename = shadow_gate.SHADOW_ARTIFACTS.get(_norm(tag), {}).get(_norm(direction))
    if not filename:
        return None
    claims = slot_claims() if claims is None else claims

    reasons: list[str] = []
    foreign = sorted(claims.get(filename, set()) - {_norm(tag)})
    if foreign:
        reasons.append(f"Root-Slot wird auch von {', '.join(foreign)} gelesen")
    if not filename.lower().startswith(tag_prefix(tag) + "_"):
        reasons.append(f"Dateiname trägt nicht den tag-eigenen Präfix '{tag_prefix(tag)}_'")
    if not reasons:
        return None

    live = shadow_gate.is_live(tag, direction)
    severity = FAIL if live else WARN
    reasons.append(
        "Bein ist LIVE ⇒ shadow_artifact_path liefert diesen Root-Namen bereits"
        if live
        else f"Bein ist {shadow_gate.leg_status(tag, direction)} ⇒ heute latent, blockiert die nächste Promotion"
    )
    return Finding(
        tag=_norm(tag),
        direction=_norm(direction),
        filename=filename,
        severity=severity,
        reasons=tuple(reasons),
        suggestion=suggested_name(tag, filename),
    )


def scan() -> list[Finding]:
    """Alle Beine aus `SHADOW_ARTIFACTS`, stabil sortiert (FAIL zuerst)."""
    claims = slot_claims()
    findings = [
        f
        for tag in sorted(shadow_gate.SHADOW_ARTIFACTS)
        for direction in sorted(shadow_gate.SHADOW_ARTIFACTS[tag])
        if (f := check_leg(tag, direction, claims)) is not None
    ]
    findings.sort(key=lambda f: (f.severity != FAIL, f.tag, f.direction))
    return findings


def check_staging_filename(filename: str, claims: dict[str, set[str]] | None = None) -> tuple[str, str]:
    """Promotions-Vorschau für EINE Staging-Datei (Einstieg für den Verifier).

    Der Verifier sieht nur die Datei, nicht die Absicht — wer `rub2_model_LONG.pkl`
    promotet, kann die RUB2-Generation ODER den RUB3-Challenger meinen. Deshalb
    hier bewusst WARN statt FAIL: der Befund nennt die konkurrierenden Tags, das
    Urteil bleibt beim Operator. Der unzweideutige Fall (Register sagt LIVE) ist
    `scan()` und dort ein FAIL."""
    claims = slot_claims() if claims is None else claims
    tags = sorted(claims.get(filename, set()))
    if len(tags) < 2:
        return OK, f"Root-Slot '{filename}' wird nur von {tags[0] if tags else '—'} beansprucht"
    hint = " / ".join(f"{t} → {suggested_name(t, filename)}" for t in tags)
    return WARN, (
        f"Root-Slot '{filename}' wird von {len(tags)} Tags beansprucht ({', '.join(tags)}) — "
        f"eine Promotion bedient beide Loader aus EINER Datei (Regel-4-Doppel-Post-Hazard). "
        f"Challenger challenger-distinkt benennen: {hint}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Challenger-Promotion-Namensguard: kapert ein Challenger-Artefakt einen fremden Loader-Slot?"
    )
    parser.add_argument("--strict", action="store_true", help="WARNs ebenfalls als Fehler werten (exit 1)")
    args = parser.parse_args(argv)

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    findings = scan()
    if not findings:
        print("PASS promotion-guard: alle Challenger-Artefakte tragen einen challenger-distinkten Root-Namen.")
        return 0
    for f in findings:
        print(f.as_line())
    fails = [f for f in findings if f.severity == FAIL]
    if fails:
        print(f"\n{len(fails)} FAIL — ein LIVE-Bein lädt aus einem fremden Root-Slot. Artefakt umbenennen (Regel 4/6).")
        return 1
    if args.strict:
        print(f"\n{len(findings)} WARN und --strict — als Fehler gewertet.")
        return 1
    print(f"\n{len(findings)} WARN, kein FAIL — latente Promotions-Blocker, heute ohne Live-Effekt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
