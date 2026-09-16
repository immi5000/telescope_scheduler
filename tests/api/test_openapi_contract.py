"""The frontend's types are generated from a committed OpenAPI document.

That document is the contract, and a contract nobody checks is a comment. This
test fails the moment the server's schema and the committed copy disagree, so
the failure lands on the person who changed the API rather than on whoever next
runs the UI and sees an undefined field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tscheduler.api.app import create_app

pytest.importorskip("fastapi", reason="server tests need the api extra: uv sync --extra api")

SPEC = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


def test_committed_openapi_matches_the_app() -> None:
    if not SPEC.is_file():
        pytest.skip("frontend/ not present")
    committed = json.loads(SPEC.read_text())
    current = json.loads(json.dumps(create_app().openapi(), sort_keys=True))

    if committed == current:
        return

    cs = set(committed.get("components", {}).get("schemas", {}))
    ns = set(current.get("components", {}).get("schemas", {}))
    detail = [
        f"  schemas added:   {sorted(ns - cs)}" if ns - cs else "",
        f"  schemas removed: {sorted(cs - ns)}" if cs - ns else "",
        *(
            f"  fields changed in {name}: "
            f"+{sorted(fields_of(current, name) - fields_of(committed, name))} "
            f"-{sorted(fields_of(committed, name) - fields_of(current, name))}"
            for name in sorted(cs & ns)
            if fields_of(committed, name) != fields_of(current, name)
        ),
        f"  paths: {sorted(set(current['paths']) ^ set(committed['paths']))}"
        if set(current["paths"]) != set(committed["paths"])
        else "",
    ]
    raise AssertionError(
        "frontend/openapi.json is out of date. Regenerate it and the TypeScript "
        "types:\n"
        '  uv run python -c "import json,pathlib;'
        "from tscheduler.api.app import create_app;"
        "pathlib.Path('frontend/openapi.json').write_text("
        'json.dumps(create_app().openapi(), indent=2, sort_keys=True)+chr(10))"\n'
        "  cd frontend && npm run gen:types\n" + "\n".join(d for d in detail if d)
    )


def fields_of(spec: dict[str, object], name: str) -> set[str]:
    schemas = spec.get("components", {}).get("schemas", {})  # type: ignore[union-attr]
    return set(schemas.get(name, {}).get("properties", {}))
