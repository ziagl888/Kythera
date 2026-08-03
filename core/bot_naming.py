"""
Central bot naming normalisation.

Different modules (bots, monitors, market tracker, regime analyzer)
have historically written different strings to the DB:

  - Classic bots:     "Fast In And Out", "Support Resistance",
                      "Volume Indicator", "5 Percent"
  - MIS bot models:   "MIS1-8H", "MIS1-8h_pump", "MIS1-8h_dump",
                      "MIS1-24H", "MIS1-168H" etc.
  - Legacy typo:      "MSI1-*" (historical, bot fixed)

So dashboards/reports aggregate consistently, all consumers normalise
names via pretty_name() — and writers can also use normalised names
directly on upsert.

IMPORTANT: The normalisation is idempotent — pretty_name(pretty_name(x))
== pretty_name(x). An already-normalised name stays unchanged.
"""

import re as _re

# Display aliases for classic bots — spaces removed for
# better readability in tables
_CLASSIC_ALIASES = {
    "Fast In And Out": "FastInOut",
    "Support Resistance": "SR",
    "Volume Indicator": "VolIndic",
    "5 Percent": "5Percent",
}

# Pre-compiled regex for MIS consolidation — cross-generation (MIS1,
# MIS2, ... — operator versioning rule 2026-07-06: retrains post under a
# new tag). Fully case-insensitive: the orchestrator matches bot names
# with re.IGNORECASE from message text ("MIS1-8H_Pump" is possible) — every
# case variant must normalise to the same whitelist key.
_MIS1_PATTERN = _re.compile(r'^(MIS\d+-\d+)h(?:_(?:pump|dump))?$', _re.IGNORECASE)


def pretty_name(s: str) -> str:
    """Normalises a bot/strategy name to its canonical form.

    Idempotent — the normalised name is stable across repeated application.

    Transformations:
      1. MSI1-* → MIS1-*  (historical typo fix)
      2. MIS1-<N>H → MIS1-<N>h  (case consolidation, lowercase h)
      3. MIS1-<N>h_pump / MIS1-<N>h_dump → MIS1-<N>h  (pump/dump consolidated)
      4. Classic bot names → short form for tables

    Examples:
        pretty_name("Fast In And Out")       == "FastInOut"
        pretty_name("MIS1-8H")               == "MIS1-8h"
        pretty_name("MIS1-168H_pump")        == "MIS1-168h"
        pretty_name("MIS2-72H")              == "MIS2-72h"   # new generation
        pretty_name("MSI1-24h")              == "MIS1-24h"
        pretty_name("ATS1")                  == "ATS1"          # unchanged
        pretty_name("ATS1_Robust")           == "ATS1_Robust"   # unchanged
        pretty_name("FastInOut")             == "FastInOut"     # idempotent
    """
    if s is None:
        return ""
    s = str(s).strip()
    if not s:
        return ""

    # 1. Typo fix MSI1 → MIS1
    if s.startswith("MSI1-"):
        s = "MIS1-" + s[len("MSI1-") :]
    elif s == "MSI1":
        s = "MIS1"

    # 2+3. Consolidate MIS1-<N>H + pump/dump (case-insensitive; group(1)
    # carries the original casing of the input, hence upper() for the canon)
    m = _MIS1_PATTERN.match(s)
    if m:
        s = m.group(1).upper() + "h"

    # 4. Classic aliases
    return _CLASSIC_ALIASES.get(s, s)
