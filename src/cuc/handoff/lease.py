"""A single control lease per run, persisted so another process can see who is in control."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


class Controller(StrEnum):
    AUTOMATION = "automation"
    HUMAN = "human"


class HandoffState(StrEnum):
    AUTOMATION = "AUTOMATION"  # automation holds the lease and is acting
    PAUSED = "PAUSED"          # automation stopped acting, intervention request written, lease not yet transferred
    HUMAN = "HUMAN"            # human holds the lease and drives the same live session
    RESUMING = "RESUMING"      # human signalled done; automation re-verifies before acting


@dataclass
class ControlLease:
    run_id: str
    holder: Controller = Controller.AUTOMATION
    state: HandoffState = HandoffState.AUTOMATION
    version: int = 0
    since: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    intervention_id: str | None = None
    resume: dict[str, Any] | None = None  # {"decision": ..., "note": ..., "at": ...} written by the operator CLI

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["holder"], d["state"] = self.holder.value, self.state.value
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ControlLease":
        return cls(run_id=d["run_id"], holder=Controller(d["holder"]), state=HandoffState(d["state"]), version=int(d.get("version", 0)),
                   since=d.get("since", ""), intervention_id=d.get("intervention_id"), resume=d.get("resume"))


class LeaseFile:
    """control.json in the run directory. The replay/discovery process owns writes except the `resume` field."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / "control.json"

    def read(self) -> ControlLease | None:
        if not self.path.exists():
            return None
        return ControlLease.from_json(json.loads(self.path.read_text()))

    def write(self, lease: ControlLease) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(lease.to_json(), indent=2))
        tmp.replace(self.path)

    def signal_resume(self, decision: str, note: str = "", by: str = "operator") -> ControlLease:
        """Called by the operator CLI. Only valid while a human holds the lease."""
        lease = self.read()
        if lease is None:
            raise FileNotFoundError(f"no control lease at {self.path}")
        if lease.state is not HandoffState.HUMAN:
            raise RuntimeError(f"cannot resume: run is in state {lease.state.value}, not HUMAN")
        if decision not in ("resumed", "confirmed", "abandoned"):
            raise ValueError("decision must be resumed | confirmed | abandoned")
        lease.resume = {"decision": decision, "note": note, "by": by, "at": datetime.now(timezone.utc).isoformat()}
        self.write(lease)
        return lease
