"""Agent-facing capability catalog (stretch goal).

Saved artifacts are exposed as typed tools: name, description, JSON Schema for
inputs and outputs, plus the business outcome codes a caller must handle. An AI
agent discovers capabilities here and invokes one by name with typed args; the
invocation is a deterministic replay, never a model call.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from cuc.schema import Artifact, RunResult


@dataclass(frozen=True)
class CapabilityTool:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    outcome_codes: list[str]
    irreversible: bool
    path: str

    def as_tool_definition(self) -> dict[str, Any]:
        """Function-calling shape a planner can hand to a model."""
        outcomes = ", ".join(self.outcome_codes) or "none"
        return {"name": self.name,
                "description": f"{self.description} Returns {sorted(self.output_schema['properties'])}. "
                               f"Possible business outcomes: {outcomes}." + (" Contains an irreversible step; requires human confirmation." if self.irreversible else ""),
                "input_schema": self.input_schema}


class Catalog:
    def __init__(self, artifacts_dir: Path):
        self.dir = artifacts_dir

    def load_all(self) -> dict[str, Artifact]:
        out: dict[str, Artifact] = {}
        for p in sorted(self.dir.glob("*.json")):
            try:
                a = Artifact.model_validate_json(p.read_text())
            except ValidationError:
                continue  # not a capability artifact (or an invalid one); never expose it
            # newest version wins when several files share a capability id
            if a.capability_id not in out or _semver(a.version) > _semver(out[a.capability_id].version):
                out[a.capability_id] = a
        return out

    def tools(self) -> list[CapabilityTool]:
        tools = []
        for a in self.load_all().values():
            tools.append(CapabilityTool(
                name=a.capability_id, version=a.version, description=a.description,
                input_schema=a.input_json_schema(), output_schema=a.output_json_schema(),
                outcome_codes=[d.code for d in a.outcome_detectors],
                irreversible=any(s.risk_class == "irreversible" for s in a.steps),
                path=str(self._path_for(a))))
        return tools

    def get(self, name: str) -> Artifact:
        arts = self.load_all()
        if name not in arts:
            raise KeyError(f"unknown capability {name!r}; known: {sorted(arts)}")
        return arts[name]

    def invoke(self, name: str, args: dict[str, Any], runner: Callable[[Artifact, dict[str, Any]], RunResult]) -> RunResult:
        """Validate args against the contract, then hand the artifact to the deterministic runner."""
        a = self.get(name)
        a.validate_params(args)
        return runner(a, args)

    def _path_for(self, a: Artifact) -> Path:
        for p in self.dir.glob("*.json"):
            if a.capability_id in p.name:
                return p
        return self.dir / f"{a.capability_id}.json"


def _semver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))
