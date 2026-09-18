"""Export JSON Schema for the artifact and result contracts to /schema."""
from __future__ import annotations

import json
from pathlib import Path

from .artifact import Artifact
from .result import RunResult

FILES = {"artifact.schema.json": Artifact, "run_result.schema.json": RunResult}


def render() -> dict[str, str]:
    out = {}
    for fname, model in FILES.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        out[fname] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out


def export(schema_dir: Path) -> list[Path]:
    schema_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for fname, text in render().items():
        p = schema_dir / fname
        p.write_text(text)
        written.append(p)
    return written
