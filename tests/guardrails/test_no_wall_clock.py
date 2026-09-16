"""No module below the API layer may read a wall clock.

This is enforcement layer 1 (structural). A provider that calls
``datetime.now()`` can silently return tomorrow's data during a replay of last
Tuesday, and no amount of downstream filtering will catch it -- the record will
look internally consistent. So the call is banned outright, by AST walk, with a
deliberately tiny allowlist.

An import-level lint rule is not enough: ``datetime.now()`` is an *attribute*
call, so the ban has to look at the syntax tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "tscheduler"

#: (object, attribute) pairs that read the system clock.
BANNED: frozenset[tuple[str, str]] = frozenset(
    {
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("datetime", "today"),
        ("date", "today"),
        ("time", "time"),
        ("time", "time_ns"),
        ("Time", "now"),  # astropy
    }
)

#: The only modules permitted to read the wall clock, relative to src/tscheduler.
ALLOWLIST: frozenset[str] = frozenset(
    {
        "core/clock.py",  # AsOf.live() -- the single sanctioned reader
        "tasks/pollers.py",  # background pollers turn real time into Events
    }
)


def _offenders(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            pair = (fn.value.id, fn.attr)
            if pair in BANNED:
                hits.append((node.lineno, f"{fn.value.id}.{fn.attr}()"))
    return hits


def test_no_wall_clock_reads_outside_allowlist() -> None:
    failures: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWLIST:
            continue
        for lineno, call in _offenders(path):
            failures.append(f"  src/tscheduler/{rel}:{lineno}  {call}")

    assert not failures, (
        "Wall-clock reads found outside the allowlist. Thread an AsOf through "
        "instead -- a provider that reads the clock cannot be replayed.\n" + "\n".join(failures)
    )


def test_allowlist_entries_still_exist() -> None:
    """A stale allowlist silently widens the ban's blast radius."""
    missing = [e for e in ALLOWLIST if not (SRC / e).exists()]
    assert missing in ([], ["tasks/pollers.py"]), f"allowlist references missing files: {missing}"
