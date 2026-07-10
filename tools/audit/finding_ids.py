"""Deterministic finding-ID allocation and duplicate detection for AUDIT_TODO.md.

Background (T-2026-CU-9050-059): on 2026-07-09/10 three freshly written findings
all carried the id P1.46. Several sessions worked the ledger in parallel; each read
it, took what looked like the next free number, and wrote it back. A classic
read-modify-write race with no allocator. PR #36 renumbered them by hand
(P1.47/P1.48) but left the cause standing.

The KB solved the same problem for task ids with next_id(), which scrolls
deterministically over every matching doc instead of guessing max(NNN) from a
capped search (the lesson of T-2026-CU-9021-001). This is that, for the ledger.

Two subcommands, and the cheap one is the important one:

    check                 exit 1 if any finding id is defined twice.  <- the net
    next --severity P1    print the next free id for that severity.   <- convenience

`next` is a snapshot, not a reservation — exactly like next_id(). Two sessions
calling it at the same instant still get the same number. What stops the collision
from reaching main is `check`, wired into pre-commit.

DEFINITION vs REFERENCE — the whole subtlety of this file. Findings are
cross-referenced in prose all over the ledger ("orthogonal to P1.44", "siehe
P2.2"), so a naive grep for P\\d+\\.\\d+ reports dozens of "duplicates" and the
check is red forever. A finding is DEFINED on exactly one kind of line: a markdown
checkbox whose first bold token is the id.

    - [ ] **P1.45 Post-Pfade verwerfen die Artefakt-model_id** ...
    - [x] **P0.1 ~(Step2: ...) Outbox ist at-least-once** ...

Everything else is a reference and is ignored.

Run:
    python tools/audit/finding_ids.py check
    python tools/audit/finding_ids.py next --severity P1
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

# A finding DEFINITION: list item, checkbox (checked or not), then the bold id.
# The trailing (?!\d) stops P1.4 from matching inside P1.45.
DEFINITION_RE = re.compile(r"^\s*-\s+\[[ xX]\]\s+\*\*P(\d+)\.(\d+)(?!\d)")

DEFAULT_LEDGER = pathlib.Path(__file__).resolve().parents[2] / "AUDIT_TODO.md"
SEVERITY_RE = re.compile(r"^P(\d+)$")
# Any finding id anywhere in the text — definition OR prose reference. Used only
# to detect that the definition regex has gone blind (see _detect_format_drift).
ANY_ID_RE = re.compile(r"\bP\d+\.\d+\b")


def parse_definitions(text: str) -> list[tuple[int, int, int]]:
    """Return (severity, number, line_no) for every finding DEFINITION line."""
    out = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        m = DEFINITION_RE.match(line)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), line_no))
    return out


def _id_sort_key(finding_id: str) -> tuple[int, int]:
    """"P1.9" -> (1, 9). Lexical sort would put P1.10 before P1.9."""
    sev, num = finding_id.lstrip("P").split(".")
    return int(sev), int(num)


def find_duplicates(defs: list[tuple[int, int, int]]) -> dict[str, list[int]]:
    """Map "P1.46" -> [line, line, ...] for every id defined more than once."""
    seen: dict[str, list[int]] = collections.defaultdict(list)
    for sev, num, line_no in defs:
        seen[f"P{sev}.{num}"].append(line_no)
    return {fid: lines for fid, lines in seen.items() if len(lines) > 1}


def next_free(defs: list[tuple[int, int, int]], severity: int) -> str:
    """Highest number defined for this severity, plus one. Empty severity starts at 1."""
    used = [num for sev, num, _ in defs if sev == severity]
    return f"P{severity}.{max(used) + 1 if used else 1}"


def _read_ledger(path: pathlib.Path) -> str | None:
    """Ledger text, or None when the file is absent/unreadable.

    Deliberately fail-open: this runs as a pre-commit hook on every commit, and a
    checkout without AUDIT_TODO.md (or a worktree mid-rebase) must not block the
    commit. Only a real, determinable duplicate blocks — same philosophy as the
    other Cu gates.

    UnicodeDecodeError is caught alongside OSError on purpose: it is a ValueError,
    not an OSError, so an uncaught one would crash the hook and block every commit —
    the exact opposite of failing open.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def detect_format_drift(text: str, defs: list[tuple[int, int, int]]) -> bool:
    """True when the ledger mentions finding ids but we parsed zero definitions.

    Without this, a change to the ledger's markdown (say `* [ ]` instead of `- [ ]`)
    makes DEFINITION_RE match nothing, and `check` cheerfully reports "0 findings, no
    duplicates" forever — a guard that disarms itself in silence. Same failure class
    as P2.51 (the regression guard going quiet when its goldens vanish). An empty or
    id-less file is NOT drift; it is simply a ledger without findings.
    """
    return not defs and bool(ANY_ID_RE.search(text))


def cmd_check(args: argparse.Namespace) -> int:
    text = _read_ledger(args.ledger)
    if text is None:
        print(f"[finding-ids] {args.ledger} nicht lesbar — skip (fail-open).")
        return 0

    defs = parse_definitions(text)

    if detect_format_drift(text, defs):
        print(
            f"[finding-ids] FEHLER — {args.ledger} nennt Finding-IDs, aber KEINE einzige "
            "Definitionszeile wurde erkannt.\n"
            "  Das Ledger-Format hat sich geaendert und der Guard ist blind — er wuerde\n"
            "  ab jetzt jede Kollision durchwinken. DEFINITION_RE in tools/audit/finding_ids.py\n"
            "  ans neue Format anpassen (erwartet: `- [ ] **P1.45 …`).",
            file=sys.stderr,
        )
        return 1

    dupes = find_duplicates(defs)
    if not dupes:
        print(f"[finding-ids] OK — {len(defs)} Findings, keine doppelte ID.")
        return 0

    print(f"[finding-ids] FEHLER — {len(dupes)} doppelt vergebene Finding-ID(s):", file=sys.stderr)
    # numerisch sortieren, nicht lexikalisch — sonst steht P1.10 vor P1.9
    for fid, lines in sorted(dupes.items(), key=lambda kv: _id_sort_key(kv[0])):
        where = ", ".join(f"Zeile {n}" for n in lines)
        print(f"  {fid}: {where}", file=sys.stderr)
    print(
        "\n  Zwei Sessions haben dieselbe Nummer gegriffen. Die naechste freie ID liefert:\n"
        "    python tools/audit/finding_ids.py next --severity P<n>",
        file=sys.stderr,
    )
    return 1


def cmd_next(args: argparse.Namespace) -> int:
    m = SEVERITY_RE.match(args.severity)
    if not m:
        print(f"[finding-ids] --severity erwartet P0..P3, bekam {args.severity!r}", file=sys.stderr)
        return 2

    text = _read_ledger(args.ledger)
    if text is None:
        print(f"[finding-ids] {args.ledger} nicht lesbar.", file=sys.stderr)
        return 2

    print(next_free(parse_definitions(text), int(m.group(1))))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--ledger",
        type=pathlib.Path,
        default=DEFAULT_LEDGER,
        help="Pfad zum Ledger (default: AUDIT_TODO.md im Repo-Root)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="Exit 1, wenn eine Finding-ID doppelt definiert ist")
    c.set_defaults(func=cmd_check)

    n = sub.add_parser("next", help="Naechste freie Finding-ID einer Severity drucken")
    n.add_argument("--severity", required=True, help="P0 | P1 | P2 | P3")
    n.set_defaults(func=cmd_next)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
