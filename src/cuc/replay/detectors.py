"""Post-step state classification, in the order the contract promises:

    business outcome  ->  recoverable condition  ->  postcondition  ->  hard failure
"""
from __future__ import annotations

from dataclasses import dataclass

from cuc.schema import Artifact, Condition, OutcomeDetector, Recovery, RecoveryAction, Step
from cuc.surface.base import Surface


@dataclass
class Classification:
    outcome: OutcomeDetector | None = None
    recovery: Recovery | None = None
    postcondition_ok: bool = False

    @property
    def kind(self) -> str:
        if self.outcome:
            return "business_outcome"
        if self.recovery:
            return "recoverable"
        if self.postcondition_ok:
            return "ok"
        return "hard_failure"


def applicable_detectors(artifact: Artifact, step: Step) -> list[OutcomeDetector]:
    return [d for d in artifact.outcome_detectors if d.after_steps is None or step.id in d.after_steps]


def applicable_recoveries(artifact: Artifact) -> list[Recovery]:
    # ACCEPT_DIALOG is applied by the surface's dialog policy at dialog time, not by polling.
    return [r for r in artifact.recoveries if r.action is not RecoveryAction.ACCEPT_DIALOG]


def classify(surface: Surface, artifact: Artifact, step: Step, postcondition: Condition | None, timeout_ms: int) -> Classification:
    """Wait until the postcondition, a detector or a recovery trigger holds, then classify by priority."""
    detectors = applicable_detectors(artifact, step)
    recoveries = applicable_recoveries(artifact)
    watched: list[Condition] = [d.condition for d in detectors] + [r.trigger for r in recoveries]
    if postcondition is not None:
        watched.append(postcondition)
    # Without a postcondition there is nothing to wait for; detectors are checked once, not polled for the whole timeout.
    if postcondition is not None and watched:
        surface.wait_for_any(watched, timeout_ms)
    c = Classification()
    for d in detectors:
        if surface.check(d.condition):
            c.outcome = d
            return c
    for r in recoveries:
        if surface.check(r.trigger):
            c.recovery = r
            return c
    c.postcondition_ok = postcondition is None or surface.check(postcondition)
    return c
