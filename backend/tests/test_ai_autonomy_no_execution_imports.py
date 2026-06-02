"""Authority pin: these directories must NEVER import the execution
path, RoadGuard, or a broker adapter. The whole point of the side-band
is that a self-trained model cannot escape into a live order. If this
test fails, the merge is wrong — don't relax the assertion, fix the
import.

Detection mechanism (2026-02-17 upgrade): AST walk of every Python
file in the guarded directories, collecting:
    * `import X` and `import X.Y`
    * `from X.Y import Z`
The substring approach broke when role profiles legitimately mentioned
"Kraken readiness" in a doctrine string — describing what NOT to do is
not the same as IMPORTING the broker. AST scan only flags actual code
dependencies.
"""
import ast
from pathlib import Path

import pytest


# Modules that, if imported (or any submodule thereof), prove the
# guarded directory has reached into the execution layer.
FORBIDDEN_MODULE_PREFIXES = (
    "shared.execution",
    "shared.broker_router",
    "shared.crypto.kraken",
    "shared.crypto.broker_adapter",
    "shared.alpaca",
    "shared.broker",
    "roadguard",
)


GUARDED_DIRS = (
    "shared/ai_autonomy",
    "shared/rise_ai",
)


def _imports_in(path: Path) -> set[str]:
    """Return the set of top-level module paths imported by `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.add(node.module)
    return out


@pytest.mark.parametrize("relpath", GUARDED_DIRS)
def test_guarded_dir_has_no_execution_imports(relpath: str):
    root = Path(__file__).resolve().parent.parent / relpath
    all_imports: set[str] = set()
    for py in root.glob("*.py"):
        all_imports |= _imports_in(py)

    offenders: list[tuple[str, str]] = []
    for imp in all_imports:
        for forbidden in FORBIDDEN_MODULE_PREFIXES:
            if imp == forbidden or imp.startswith(forbidden + "."):
                offenders.append((imp, forbidden))

    assert not offenders, (
        f"{relpath} imports execution-layer modules: {offenders}. "
        "This breaks the authority firewall — the side-band must never "
        "be able to reach the broker. Fix the import, do not relax the "
        "assertion."
    )
