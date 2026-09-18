"""The intervention request handed to the operator: everything needed to act."""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class InterventionRequest:
    intervention_id: str
    run_id: str
    kind: str                      # stuck | confirm_irreversible
    capability_id: str
    description: str
    step_id: str | None
    step_label: str | None
    reason: str
    current_url: str
    screenshot: str | None
    a11y_snapshot: str | None
    log: str | None
    steps_completed: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    how_to_resume: str = ""

    @classmethod
    def new(cls, **kw) -> "InterventionRequest":
        iid = f"int-{secrets.token_hex(4)}"
        run_id = kw["run_id"]
        how = (f"The live browser window is now yours. Perform the manual step(s), then signal:\n"
               f"  cuc resume --run-id {run_id} --decision resumed   # you fixed it; automation re-verifies and continues\n"
               f"  cuc resume --run-id {run_id} --decision confirmed # (irreversible step) go ahead and perform it\n"
               f"  cuc resume --run-id {run_id} --decision abandoned # stop the run")
        return cls(intervention_id=iid, how_to_resume=how, **kw)

    def write(self, run_dir: Path) -> Path:
        p = run_dir / "intervention.json"
        p.write_text(json.dumps(asdict(self), indent=2))
        return p
