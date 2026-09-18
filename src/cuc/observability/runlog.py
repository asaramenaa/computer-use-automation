"""Structured JSONL run log. Every payload passes through the redactor first."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cuc.policy.redaction import Redactor


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


class RunLog:
    def __init__(self, run_dir: Path, run_id: str, redactor: Redactor):
        self.run_dir = run_dir
        self.run_id = run_id
        self.redactor = redactor
        self.path = run_dir / "events.jsonl"
        run_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._fh = self.path.open("a", encoding="utf-8")

    def event(self, kind: str, /, **payload: Any) -> dict[str, Any]:  # kind is positional-only: payloads may carry their own "kind"
        self._seq += 1
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id, "seq": self._seq, "event": kind}
        rec.update(self.redactor(payload))
        self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        return rec

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.run_dir / name
        p.write_text(json.dumps(self.redactor(obj), indent=2, default=str) + "\n")
        return p

    def close(self) -> None:
        self._fh.close()
