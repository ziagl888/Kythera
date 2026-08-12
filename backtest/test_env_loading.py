"""T-2026-KYT-9050-138 — pin the .env loading contract of the config-less fleet entries.

The bug class: a fleet entry script reads a KYTHERA_* gate but its import chain never
reaches core.config, so load_dotenv() never runs in that process and the documented
".env + fleet restart" operator path silently does nothing (found live 2026-08-12: the
snapshot service stayed dormant after a gate flip; only a setx user env worked).

These tests are AST-based and import nothing from the checked scripts — DB-free and
side-effect-free by construction. They pin two things per script:

1. `import core.config` exists at MODULE level (not inside a function — a lazy import
   runs too late for spawn-time env inheritance, the exact watchdog failure).
2. It precedes every other `core.*`/`from core...` import, so no module-level gate read
   in an earlier import can run before .env is loaded.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The scripts that read KYTHERA_* env but whose import chains do not reach core.config
# on their own (scan 2026-08-12). Bots are exempt: they import core.config for DB creds.
PINNED_SCRIPTS = ("candle_snapshot_service.py", "main_watchdog.py", "dashboard.py")


def _module_body_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []
    for node in tree.body:  # module body only — lazy imports inside defs excluded
        if isinstance(node, ast.Import):
            out += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
    return out


def test_pinned_scripts_import_core_config_at_module_level() -> None:
    for script in PINNED_SCRIPTS:
        imports = _module_body_imports(ROOT / script)
        assert "core.config" in [m for _, m in imports], f"{script}: no module-level import core.config"


def test_core_config_precedes_every_other_core_import() -> None:
    for script in PINNED_SCRIPTS:
        imports = _module_body_imports(ROOT / script)
        config_line = min(line for line, m in imports if m == "core.config")
        other_core = [(line, m) for line, m in imports if m.startswith("core") and m != "core.config"]
        early = [(line, m) for line, m in other_core if line < config_line]
        assert not early, f"{script}: core imports before core.config: {early}"


def test_core_config_loads_dotenv_at_import() -> None:
    # The contract the pins rely on: core/config.py itself calls load_dotenv() in its
    # module body. Checked via AST so this test never touches the real .env.
    tree = ast.parse((ROOT / "core" / "config.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "load_dotenv"
    ]
    assert calls, "core/config.py no longer calls load_dotenv() at module level"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK {name}")
    print("all tests passed")
