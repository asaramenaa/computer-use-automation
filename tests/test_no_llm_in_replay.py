"""The production path must not be able to consult a model. Enforced structurally."""
import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "cuc"
FORBIDDEN = ("anthropic", "google", "openai", "groq", "cuc.agent")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


@pytest.mark.parametrize("pkg", ["replay", "surface", "policy", "schema", "observability", "handoff"])
def test_production_packages_do_not_import_llm_clients(pkg):
    for f in (SRC / pkg).rglob("*.py"):
        bad = {m for m in _imports(f) if m.split(".")[0] in FORBIDDEN or m.startswith("cuc.agent")}
        assert not bad, f"{f} imports {bad}"


def test_importing_replay_does_not_load_llm_sdks():
    for m in list(sys.modules):
        if m.split(".")[0] in ("anthropic", "google", "openai", "groq"):
            del sys.modules[m]
    import importlib

    import cuc.replay  # noqa: F401

    importlib.reload(cuc.replay)
    loaded = [m for m in sys.modules if m.split(".")[0] in ("anthropic", "google", "openai", "groq")]
    assert not loaded, loaded
