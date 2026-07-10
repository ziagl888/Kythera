"""Standalone (DB-free) tests for the AUDIT_TODO finding-id allocator/guard.

Background (T-2026-CU-9050-059): three findings were written as P1.46 at once by
parallel sessions; PR #36 renumbered them by hand. tools/audit/finding_ids.py is
the allocator + duplicate guard that stops it happening again.

The load-bearing test here is test_prose_reference_is_not_a_definition: findings are
cross-referenced in prose throughout the ledger, so a checker that greps for the bare
id pattern reports duplicates on a perfectly healthy file and gets disabled within a
day. Only the checkbox line defines a finding.

Run: python backtest/test_finding_ids.py
"""

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "audit"))

import finding_ids  # noqa: E402

LEDGER = ROOT / "AUDIT_TODO.md"


def _tmp_ledger(text: str) -> pathlib.Path:
    fh = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    fh.write(text)
    fh.close()
    return pathlib.Path(fh.name)


def test_duplicate_definition_is_detected():
    path = _tmp_ledger(
        "- [ ] **P1.46 Erstes Finding** blah\n"
        "- [ ] **P1.47 Anderes** blah\n"
        "- [x] **P1.46 Zweites Finding mit derselben ID** blah\n"
    )
    defs = finding_ids.parse_definitions(path.read_text(encoding="utf-8"))
    dupes = finding_ids.find_duplicates(defs)
    assert "P1.46" in dupes, "doppelt definierte ID nicht erkannt"
    assert dupes["P1.46"] == [1, 3], f"falsche Zeilennummern: {dupes['P1.46']}"
    assert finding_ids.main(["--ledger", str(path), "check"]) == 1, "check muss Exit 1 liefern"
    path.unlink()


def test_clean_ledger_passes():
    path = _tmp_ledger("- [ ] **P1.46 Eins** x\n- [x] **P1.47 Zwei** y\n- [ ] **P2.3 Drei** z\n")
    assert finding_ids.main(["--ledger", str(path), "check"]) == 0
    path.unlink()


def test_prose_reference_is_not_a_definition():
    """The trap. A finding referenced in another finding's prose (or in a header,
    or in a changelog note) must NOT count as a second definition — otherwise the
    guard is red on a healthy ledger and gets switched off."""
    path = _tmp_ledger(
        "- [ ] **P1.46 Echtes Finding** orthogonal zu P1.46 und siehe auch P1.46.\n"
        "Fliesstext ueber P1.46 und **P1.46** fett mitten im Satz.\n"
        "  - Unterpunkt: P1.46 nochmal erwaehnt\n"
    )
    defs = finding_ids.parse_definitions(path.read_text(encoding="utf-8"))
    assert len(defs) == 1, f"erwartete genau EINE Definition, fand {len(defs)}"
    assert finding_ids.main(["--ledger", str(path), "check"]) == 0
    path.unlink()


def test_number_prefix_is_not_confused():
    """P1.4 must not match inside P1.45 (and vice versa)."""
    path = _tmp_ledger("- [ ] **P1.4 Vier** x\n- [ ] **P1.45 Fuenfundvierzig** y\n")
    defs = finding_ids.parse_definitions(path.read_text(encoding="utf-8"))
    assert sorted((s, n) for s, n, _ in defs) == [(1, 4), (1, 45)]
    assert finding_ids.main(["--ledger", str(path), "check"]) == 0
    path.unlink()


def test_next_free_is_max_plus_one_per_severity():
    path = _tmp_ledger("- [ ] **P1.46 a** x\n- [ ] **P1.48 b** y\n- [ ] **P2.3 c** z\n")
    defs = finding_ids.parse_definitions(path.read_text(encoding="utf-8"))
    assert finding_ids.next_free(defs, 1) == "P1.49", "next muss max+1 sein, nicht luecken-fuellend"
    assert finding_ids.next_free(defs, 2) == "P2.4"
    assert finding_ids.next_free(defs, 0) == "P0.1", "leere Severity startet bei 1"
    path.unlink()


def test_missing_ledger_fails_open():
    """The check runs as a pre-commit hook on every commit. A checkout without the
    ledger must not block the commit — only a real, determinable duplicate blocks."""
    missing = ROOT / "does_not_exist_AUDIT_TODO.md"
    assert not missing.exists()
    assert finding_ids.main(["--ledger", str(missing), "check"]) == 0


def test_real_ledger_is_clean():
    """The committed ledger must pass — it was renumbered in PR #36."""
    assert LEDGER.exists(), "AUDIT_TODO.md fehlt"
    defs = finding_ids.parse_definitions(LEDGER.read_text(encoding="utf-8"))
    assert len(defs) > 100, f"nur {len(defs)} Definitionen geparst — Regex passt nicht mehr aufs Format"
    dupes = finding_ids.find_duplicates(defs)
    assert not dupes, f"das echte Ledger hat doppelte IDs: {dupes}"


if __name__ == "__main__":
    test_duplicate_definition_is_detected()
    test_clean_ledger_passes()
    test_prose_reference_is_not_a_definition()
    test_number_prefix_is_not_confused()
    test_next_free_is_max_plus_one_per_severity()
    test_missing_ledger_fails_open()
    test_real_ledger_is_clean()
    print("OK - finding-id guard holds")
