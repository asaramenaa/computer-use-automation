"""Richer evidence on failure/escalation: screenshot + redacted accessibility snapshot."""
from __future__ import annotations

from pathlib import Path

from cuc.policy.redaction import Redactor
from cuc.schema import EvidenceRefs
from cuc.surface.base import Surface, SurfaceError

_seq = 0


def capture(surface: Surface, run_dir: Path, tag: str, redactor: Redactor, log_path: Path | None = None) -> EvidenceRefs:
    global _seq
    _seq += 1
    ev = run_dir / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    refs = EvidenceRefs(log=str(log_path) if log_path else None)
    try:
        refs.screenshot = str(surface.screenshot(ev / f"{_seq:03d}_{tag}.png"))
    except SurfaceError:
        pass
    try:
        p = surface.snapshot(ev / f"{_seq:03d}_{tag}.a11y.txt")
        p.write_text(redactor.text(p.read_text()))
        refs.a11y_snapshot = str(p)
    except SurfaceError:
        pass
    return refs
