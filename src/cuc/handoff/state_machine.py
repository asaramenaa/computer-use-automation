"""AUTOMATION -> PAUSED -> HUMAN -> RESUMING -> AUTOMATION, with the lease persisted at every transition."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .lease import ControlLease, Controller, HandoffState, LeaseFile

_ALLOWED: dict[HandoffState, set[HandoffState]] = {
    HandoffState.AUTOMATION: {HandoffState.PAUSED},
    HandoffState.PAUSED: {HandoffState.HUMAN, HandoffState.AUTOMATION},   # AUTOMATION: abandoned before transfer
    HandoffState.HUMAN: {HandoffState.RESUMING},
    HandoffState.RESUMING: {HandoffState.AUTOMATION},
}


class IllegalTransition(RuntimeError):
    pass


class HandoffMachine:
    def __init__(self, run_id: str, run_dir: Path, on_transition: Callable[[HandoffState, HandoffState, str], None] | None = None):
        self.file = LeaseFile(run_dir)
        self.lease = self.file.read() or ControlLease(run_id=run_id)
        self._on = on_transition
        self.file.write(self.lease)

    @property
    def state(self) -> HandoffState:
        return self.lease.state

    def _go(self, to: HandoffState, holder: Controller, reason: str) -> None:
        frm = self.lease.state
        if to not in _ALLOWED[frm]:
            raise IllegalTransition(f"{frm.value} -> {to.value} is not allowed")
        self.lease.state, self.lease.holder = to, holder
        self.lease.version += 1
        self.lease.since = datetime.now(timezone.utc).isoformat()
        self.file.write(self.lease)
        if self._on:
            self._on(frm, to, reason)

    def pause(self, intervention_id: str, reason: str) -> None:
        self.lease.intervention_id = intervention_id
        self.lease.resume = None
        self._go(HandoffState.PAUSED, Controller.AUTOMATION, reason)

    def grant_to_human(self) -> None:
        self._go(HandoffState.HUMAN, Controller.HUMAN, "lease transferred to human operator")

    def abandon_before_transfer(self, reason: str) -> None:
        self._go(HandoffState.AUTOMATION, Controller.AUTOMATION, reason)

    def begin_resume(self) -> None:
        self._go(HandoffState.RESUMING, Controller.AUTOMATION, "human signalled done; re-verifying state")

    def resume_complete(self) -> None:
        self._go(HandoffState.AUTOMATION, Controller.AUTOMATION, "automation back in control")

    def poll_resume(self) -> dict | None:
        """Re-read the lease file; return the operator's resume signal if present."""
        fresh = self.file.read()
        if fresh and fresh.resume and fresh.state is HandoffState.HUMAN:
            self.lease.resume = fresh.resume
            return fresh.resume
        return None
