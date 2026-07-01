"""
Zentrale Bot-Naming-Normalisierung.

Unterschiedliche Module (Bots, Monitore, Market-Tracker, Regime-Analyzer)
schreiben historisch mit unterschiedlichen Strings in die DB:

  - Klassische Bots:  "Fast In And Out", "Support Resistance",
                      "Volume Indicator", "5 Percent"
  - MIS-Bot Modelle:  "MIS1-8H", "MIS1-8h_pump", "MIS1-8h_dump",
                      "MIS1-24H", "MIS1-168H" etc.
  - Legacy-Typo:      "MSI1-*" (historisch, bot fixed)

Damit Dashboards/Reports konsistent aggregieren, normalisieren alle
Konsumenten die Namen durch pretty_name() — und Schreiber können beim
Upsert auch gleich normalisierte Namen verwenden.

WICHTIG: Die Normalisierung ist idempotent — pretty_name(pretty_name(x))
== pretty_name(x). Ein bereits normalisierter Name bleibt unverändert.
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

# Pre-compiled regex for MIS1 consolidation
_MIS1_PATTERN = _re.compile(
    r'^(MIS1-\d+)[hH](?:_(?:pump|dump|PUMP|DUMP))?$'
)


def pretty_name(s: str) -> str:
    """Normalises a bot/strategy name to its canonical form.

    Idempotent — der normalisierte Name ist stabil über mehrfache Anwendung.

    Transformationen:
      1. MSI1-* → MIS1-*  (historischer Typo-Fix)
      2. MIS1-<N>H → MIS1-<N>h  (Case-Konsolidierung, lowercase h)
      3. MIS1-<N>h_pump / MIS1-<N>h_dump → MIS1-<N>h  (Pump/Dump konsolidiert)
      4. Klassische Bot-Namen → kurze Form für Tabellen

    Examples:
        pretty_name("Fast In And Out")       == "FastInOut"
        pretty_name("MIS1-8H")               == "MIS1-8h"
        pretty_name("MIS1-168H_pump")        == "MIS1-168h"
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

    # 1. Typo-Fix MSI1 → MIS1
    if s.startswith("MSI1-"):
        s = "MIS1-" + s[len("MSI1-"):]
    elif s == "MSI1":
        s = "MIS1"

    # 2+3. MIS1-<N>H + Pump/Dump konsolidieren
    m = _MIS1_PATTERN.match(s)
    if m:
        s = m.group(1) + "h"

    # 4. Klassische Aliase
    return _CLASSIC_ALIASES.get(s, s)
