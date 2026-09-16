"""Only evaluation code may construct an oracle vantage or read actuals.

This is what keeps "planned with forecasts, scored with actuals" from quietly
becoming "planned with actuals" -- which would make every comparison number in
the evaluation meaningless while still looking perfectly plausible.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "tscheduler"
PERMITTED_PREFIXES = ("evaluation/",)


def _calls_asof_oracle(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "oracle"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "AsOf"
        ):
            hits.append(node.lineno)
    return hits


def test_only_evaluation_constructs_oracle_vantage() -> None:
    failures = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(PERMITTED_PREFIXES) or rel == "core/clock.py":
            continue
        for lineno in _calls_asof_oracle(path):
            failures.append(f"  src/tscheduler/{rel}:{lineno}  AsOf.oracle()")
    assert not failures, (
        "AsOf.oracle() outside evaluation/. Actuals must never reach a plan.\n"
        + "\n".join(failures)
    )


def test_actuals_provider_not_imported_outside_evaluation() -> None:
    failures = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(PERMITTED_PREFIXES):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            if mod and "providers.actuals" in mod:
                failures.append(f"  src/tscheduler/{rel}:{node.lineno}  imports {mod}")
    assert not failures, "providers.actuals imported outside evaluation/\n" + "\n".join(failures)
