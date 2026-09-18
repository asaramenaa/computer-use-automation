"""Deterministic replay engine: walk an artifact's steps with no model in the loop.

After each step the observed state is classified business outcome -> recoverable ->
postcondition -> hard failure. Recoveries are bounded; irreversible steps need a human
decision; hard failures capture evidence and, with a handler attached, hand the session over.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cuc.observability import RunLog, capture
from cuc.policy import Policy, SecretRef, SecretStore
from cuc.schema import (
    ActionType, Artifact, Escalation, EvidenceRefs, Failure, Outcome, Recovery, RecoveryAction, RecoveryApplied,
    RunResult, RunStatus, Step, ValueRef,
)
from cuc.surface.base import LocatorError, Surface, SurfaceError
from cuc.surface.playwright_surface import parse_value

from .detectors import classify


@dataclass
class InterventionContext:
    run_id: str
    run_dir: Path
    capability_id: str
    description: str
    step_id: str | None
    step_label: str | None
    reason: str
    kind: str  # "stuck" | "confirm_irreversible"
    evidence: EvidenceRefs
    current_url: str
    steps_completed: int


@dataclass
class InterventionResult:
    intervention_id: str
    request_path: str
    decision: str  # "resumed" (human acted, continue) | "confirmed" (irreversible step approved) | "abandoned"
    human_actions: list[dict[str, Any]] = field(default_factory=list)


class EscalationHandler(Protocol):
    def request(self, ctx: InterventionContext) -> InterventionResult: ...


class _Stop(Exception):
    def __init__(self, result: RunResult):
        self.result = result


class ReplayEngine:
    def __init__(self, surface: Surface, policy: Policy, log: RunLog, secrets: SecretStore, run_dir: Path,
                 escalation: EscalationHandler | None = None):
        self.surface = surface
        self.policy = policy
        self.log = log
        self.secrets = secrets
        self.run_dir = run_dir
        self.escalation = escalation
        self._recoveries_applied: list[RecoveryApplied] = []
        self._recovery_attempts: dict[str, int] = {}
        self._escalated_steps: set[str] = set()
        self._outputs: dict[str, Any] = {}
        self._started = datetime.now(timezone.utc)
        self._steps_completed = 0

    # ---------------------------------------------------------------- run
    def run(self, artifact: Artifact, params: dict[str, Any]) -> RunResult:
        self.artifact = artifact
        self.log.event("replay_started", capability_id=artifact.capability_id, version=artifact.version,
                       params={k: v for k, v in params.items()}, entry_url=artifact.app.entry_url)
        try:
            params = artifact.validate_params(params)
            d = self.policy.check_url(artifact.app.entry_url)
            if not d.allowed:
                raise _Stop(self._failed(None, "entry url allowed by policy", d.reason, EvidenceRefs(log=str(self.log.path))))
            self.surface.set_dialog_policy([r.trigger.pattern or "" for r in artifact.recoveries if r.action is RecoveryAction.ACCEPT_DIALOG])
            self._walk(artifact, params)
        except _Stop as stop:
            result = stop.result
        else:
            result = self._result(RunStatus.SUCCESS)
        self.log.event("replay_finished", status=result.status.value, steps_completed=result.steps_completed,
                       outputs=result.outputs, outcome=result.outcome.code if result.outcome else None)
        self.log.write_json("result.json", result.model_dump(mode="json"))
        return result

    def _walk(self, artifact: Artifact, params: dict[str, Any]) -> None:
        steps = artifact.steps
        i = 0
        skip_action = False
        while i < len(steps):
            step = steps[i]
            try:
                i, skip_action = self._step(artifact, params, step, i, skip_action)
            except _HardFailure as hf:
                # One path for every hard failure: evidence, then the escalation seam if a handler is attached.
                i, skip_action = self._hard_failure(step, i, hf.expected, hf.observed)

    def _step(self, artifact: Artifact, params: dict[str, Any], step: Step, i: int, skip_action: bool) -> tuple[int, bool]:
        self.surface.drain_dialogs()
        self.log.event("step_started", step_id=step.id, label=step.label, action=step.action.value, risk=step.risk_class.value)
        action_error: str | None = None
        if not skip_action:
            try:
                self._gate(step, params)
            except _SkipStep:
                pass  # a human performed this step during the handoff; verify its result below
            else:
                if step.precondition is not None and not self.surface.wait_for(step.precondition, step.timeout_ms):
                    action_error = f"precondition not met: {step.precondition.description or step.precondition.kind.value}"
                else:
                    try:
                        self._act(step, params)
                    except (LocatorError, SurfaceError) as e:
                        action_error = str(e)
                        self.log.event("action_failed", step_id=step.id, error=action_error, attempts=getattr(e, "attempts", None))
        wait_ms = 0 if action_error else step.timeout_ms
        c = classify(self.surface, artifact, step, None if action_error else step.postcondition, wait_ms)
        self.log.event("step_classified", step_id=step.id, classification=c.kind,
                       outcome=c.outcome.code if c.outcome else None, recovery=c.recovery.id if c.recovery else None,
                       dialogs=[d.__dict__ for d in self.surface.drain_dialogs()])
        if c.outcome is not None:
            raise _Stop(self._result(RunStatus.BUSINESS_OUTCOME, outcome=Outcome(code=c.outcome.code, description=c.outcome.description, step_id=step.id)))
        if c.recovery is not None:
            return self._recover(c.recovery, step, i, action_error is not None)
        if action_error or not c.postcondition_ok:
            pc = step.postcondition
            expected = (pc.description or f"{pc.kind.value} {pc.text or pc.pattern or ''}".strip()) if pc else "step completes"
            raise _HardFailure(expected, action_error or f"postcondition did not hold; {self._observed()}")
        self._checkpoints(step)
        self._steps_completed = i + 1
        self.log.event("step_completed", step_id=step.id)
        return i + 1, False

    # -------------------------------------------------------------- pieces
    def _gate(self, step: Step, params: dict[str, Any]) -> None:
        url = self.surface.current_url()
        if step.action is ActionType.NAVIGATE and step.value is not None and step.value.kind != "secret":
            url = self._value(step.value, params)[0]
        d = self.policy.check_action(step.action, url)
        if not d.allowed:
            raise _Stop(self._failed(step, "action permitted by policy", d.reason, self._evidence(step, "policy_denied")))
        control_name = next((l.name for l in step.locators if l.name), None)
        risk = self.policy.classify(step.action, None, control_name, step.risk_class)
        g = self.policy.gate(risk)
        if g.allowed:
            return
        self.log.event("risk_gate", step_id=step.id, risk=risk.value, reason=g.reason)
        if not g.requires_human or self.escalation is None:
            raise _Stop(self._failed(step, "irreversible step approved", g.reason, self._evidence(step, "risk_blocked")))
        res = self._escalate(step, g.reason, "confirm_irreversible")
        if res.decision == "confirmed":
            return
        if res.decision == "resumed" and step.postcondition is not None and self.surface.check(step.postcondition):
            self.log.event("step_completed_by_human", step_id=step.id)
            raise _SkipStep()
        raise _Stop(self._escalated(step, res, g.reason))

    def _act(self, step: Step, params: dict[str, Any]) -> None:
        value, is_secret = self._value(step.value, params) if step.value is not None else (None, False)
        target = self.surface.resolve(step.locators, step.timeout_ms) if step.locators else None
        if target is not None:
            self.log.event("target_resolved", step_id=step.id, strategy=target.strategy.value, frame=target.frame, target=target.description)
        if step.action is ActionType.EXTRACT:
            assert step.extract is not None
            raw = self.surface.extract(step.extract, step.timeout_ms)
            self._outputs[step.extract.output] = parse_value(raw, step.extract.parse)
            self.log.event("extracted", step_id=step.id, output=step.extract.output, value=self._outputs[step.extract.output])
            return
        self.surface.act(step.action, target, value, step.timeout_ms)
        self.log.event("acted", step_id=step.id, action=step.action.value, value=("[secret]" if is_secret else value))

    def _value(self, ref: ValueRef, params: dict[str, Any]) -> tuple[str, bool]:
        if ref.kind == "literal":
            return (ref.value or "").format_map(params), False
        if ref.kind == "secret":
            return self.secrets.resolve(SecretRef(ref.name or "")).reveal(), True
        v = params[ref.name]
        sref = SecretRef.parse(v) if isinstance(v, str) else None
        if sref:
            return self.secrets.resolve(sref).reveal(), True
        return str(v), False

    def _checkpoints(self, step: Step) -> None:
        for cp in self.artifact.checkpoints:
            if cp.after_step != step.id:
                continue
            ok = self.surface.wait_for(cp.condition, step.timeout_ms)
            self.log.event("checkpoint", checkpoint_id=cp.id, step_id=step.id, ok=ok, description=cp.description)
            if not ok:
                raise _HardFailure(f"checkpoint {cp.id}: {cp.description or cp.condition.kind.value}", self._observed())

    def _recover(self, r: Recovery, step: Step, i: int, action_failed: bool) -> tuple[int, bool]:
        n = self._recovery_attempts.get(r.id, 0) + 1
        self._recovery_attempts[r.id] = n
        if n > r.max_attempts:
            raise _HardFailure(f"recovery {r.id} ({r.description}) within {r.max_attempts} attempts",
                               f"trigger still present after {n - 1} attempts: {self._observed()}")
        self._recoveries_applied.append(RecoveryApplied(recovery_id=r.id, step_id=step.id, attempt=n, trigger=r.description))
        self.log.event("recovery_applied", recovery_id=r.id, action=r.action.value, step_id=step.id, attempt=n)
        steps = self.artifact.steps
        if r.action is RecoveryAction.DISMISS:
            target = self.surface.resolve(r.locators, step.timeout_ms)
            self.surface.act(ActionType.CLICK, target, None, step.timeout_ms)
            return i, not action_failed  # re-act only if the original action never happened
        if r.action is RecoveryAction.REAUTH:
            return self.artifact.step_index(r.goto_step or step.id), False
        # RETRY rewinds to the last verified state so form values are re-entered, not re-clicked on a dead page.
        self._backoff(r.backoff_ms)
        anchor = next((j for j in range(i - 1, -1, -1) if steps[j].postcondition is not None), None)
        if step.action is not ActionType.NAVIGATE:
            try:
                self.surface.history_back(step.timeout_ms)
            except SurfaceError as e:
                self.log.event("recovery_note", recovery_id=r.id, note=str(e))
        if anchor is not None:
            if not self.surface.wait_for(steps[anchor].postcondition, step.timeout_ms):  # type: ignore[arg-type]
                raise _HardFailure(f"return to verified state after {steps[anchor].id} for retry", self._observed())
            return anchor + 1, False
        return i, False

    def _hard_failure(self, step: Step, i: int, expected: str, observed: str) -> tuple[int, bool]:
        evidence = self._evidence(step, "hard_failure")
        self.log.event("hard_failure", step_id=step.id, expected=expected, observed=observed, evidence=evidence.model_dump())
        if self.escalation is None or step.id in self._escalated_steps:
            raise _Stop(self._failed(step, expected, observed, evidence))
        self._escalated_steps.add(step.id)
        res = self._escalate(step, f"{expected}; observed: {observed}", "stuck", evidence)
        if res.decision == "abandoned":
            raise _Stop(self._escalated(step, res, expected))
        # A human often completes more than the failed step: continue after the furthest step whose postcondition holds.
        steps = self.artifact.steps
        for j in range(len(steps) - 1, i - 1, -1):
            pc = steps[j].postcondition
            if pc is not None and self.surface.check(pc):
                skipped = [s.id for s in steps[i:j]]
                self.log.event("resume_verified", step_id=steps[j].id, note="state matches this step's postcondition; continuing after it",
                               steps_done_by_human=skipped + [steps[j].id])
                return j, True
        anchor = step.precondition or next((s.postcondition for s in reversed(self.artifact.steps[:i]) if s.postcondition), None)
        if anchor is None or self.surface.check(anchor):
            self.log.event("resume_verified", step_id=step.id, note="precondition holds; re-running the step")
            return i, False
        raise _Stop(self._failed(step, "session state after handoff matches the step precondition", self._observed(),
                                 self._evidence(step, "resume_mismatch")))

    def _escalate(self, step: Step, reason: str, kind: str, evidence: EvidenceRefs | None = None) -> InterventionResult:
        assert self.escalation is not None
        ctx = InterventionContext(run_id=self.log.run_id, run_dir=self.run_dir, capability_id=self.artifact.capability_id,
                                  description=self.artifact.description, step_id=step.id, step_label=step.label, reason=reason,
                                  kind=kind, evidence=evidence or self._evidence(step, kind), current_url=self.surface.current_url(),
                                  steps_completed=self._steps_completed)
        self.log.event("escalation_requested", step_id=step.id, escalation_kind=kind, reason=reason)
        res = self.escalation.request(ctx)
        self.log.event("escalation_resolved", step_id=step.id, intervention_id=res.intervention_id, decision=res.decision,
                       human_actions=len(res.human_actions))
        return res

    # ------------------------------------------------------------ helpers
    def _evidence(self, step: Step | None, tag: str) -> EvidenceRefs:
        return capture(self.surface, self.run_dir, f"{step.id if step else 'run'}_{tag}", self.log.redactor, self.log.path)

    def _observed(self) -> str:
        try:
            obs = self.surface.observe()
        except SurfaceError as e:
            return f"(observation failed: {e})"
        text = " | ".join(f"{f.path or 'top'}: {' '.join(f.text.split())[:200]}" for f in obs.frames if f.text.strip())
        return f"url={obs.url}; {text}"[:600]

    @staticmethod
    def _backoff(ms: int) -> None:
        # The one time-based wait in replay: retry backoff, never UI synchronization (those are condition polls).
        if ms > 0:
            time.sleep(ms / 1000)

    def _result(self, status: RunStatus, **kw: Any) -> RunResult:
        return RunResult(status=status, run_id=self.log.run_id, capability_id=self.artifact.capability_id,
                         version=self.artifact.version, started_at=self._started, finished_at=datetime.now(timezone.utc),
                         steps_completed=self._steps_completed, outputs=dict(self._outputs) if status is RunStatus.SUCCESS else {},
                         recoveries_applied=list(self._recoveries_applied), evidence_dir=str(self.run_dir), **kw)

    def _failed(self, step: Step | None, expected: str, observed: str, evidence: EvidenceRefs) -> RunResult:
        return self._result(RunStatus.FAILED, failure=Failure(step_id=step.id if step else None, step_label=step.label if step else None,
                                                               expected=expected, observed=observed, evidence=evidence))

    def _escalated(self, step: Step, res: InterventionResult, reason: str) -> RunResult:
        return self._result(RunStatus.ESCALATED, escalation=Escalation(intervention_id=res.intervention_id, step_id=step.id,
                                                                       reason=reason, request_path=res.request_path))


class _SkipStep(Exception):
    """Raised by the gate when a human already performed the step."""


class _HardFailure(Exception):
    def __init__(self, expected: str, observed: str):
        super().__init__(expected)
        self.expected, self.observed = expected, observed
