"""CLI operator surface. MOCK OF THE OPERATOR CONSOLE.

The handoff mechanism is real: automation pauses, the lease moves to the human,
the human drives the *same* headed browser, signals resume via `cuc resume`
(a file-based signal so it works from another terminal), their actions are
recorded, and control returns. What is mocked is the UI around it: instructions
are printed to stdout instead of pushed to an operator console, and the
resume signal is a CLI command instead of a button.
"""
from __future__ import annotations

import time
from pathlib import Path

from cuc.observability import RunLog
from cuc.replay.executor import InterventionContext, InterventionResult
from cuc.surface.playwright_surface import PlaywrightSurface

from .human_recorder import HumanActionRecorder
from .intervention import InterventionRequest
from .state_machine import HandoffMachine


class CliEscalationHandler:
    def __init__(self, surface: PlaywrightSurface, log: RunLog, run_dir: Path, timeout_s: float = 900, poll_ms: int = 500,
                 announce=print):
        self.surface, self.log, self.run_dir, self.timeout_s, self.poll_ms, self.announce = surface, log, run_dir, timeout_s, poll_ms, announce
        self.machine = HandoffMachine(log.run_id, run_dir, on_transition=self._log_transition)
        self.recorder = HumanActionRecorder(surface.page, self._on_human_event)

    def _log_transition(self, frm, to, reason):
        self.log.event("handoff_transition", **{"from": frm.value, "to": to.value, "reason": reason, "holder": self.machine.lease.holder.value,
                                                "lease_version": self.machine.lease.version})

    def _on_human_event(self, ev):
        self.log.event("human_action", **ev)

    def request(self, ctx: InterventionContext) -> InterventionResult:
        req = InterventionRequest.new(run_id=ctx.run_id, kind=ctx.kind, capability_id=ctx.capability_id, description=ctx.description,
                                      step_id=ctx.step_id, step_label=ctx.step_label, reason=ctx.reason, current_url=ctx.current_url,
                                      screenshot=ctx.evidence.screenshot, a11y_snapshot=ctx.evidence.a11y_snapshot, log=ctx.evidence.log,
                                      steps_completed=ctx.steps_completed)
        path = req.write(self.run_dir)
        self.machine.pause(req.intervention_id, ctx.reason)
        self.log.event("intervention_requested", intervention_id=req.intervention_id, path=str(path), intervention_kind=ctx.kind, step_id=ctx.step_id)
        self.announce(f"\n=== HUMAN INTERVENTION REQUIRED ({ctx.kind}) ===\n"
                      f"capability: {ctx.capability_id}\nstep: {ctx.step_id} {ctx.step_label}\nreason: {ctx.reason}\n"
                      f"screenshot: {ctx.evidence.screenshot}\nrequest: {path}\n\n{req.how_to_resume}\n")
        self.machine.grant_to_human()
        self.recorder.start()
        deadline = time.monotonic() + self.timeout_s
        signal = None
        while time.monotonic() < deadline:
            signal = self.machine.poll_resume()
            if signal:
                break
            self.surface.page.wait_for_timeout(self.poll_ms)  # pumps Playwright events so human actions are received
        human_actions = self.recorder.stop()
        self.machine.begin_resume()
        if signal is None:
            self.log.event("handoff_timeout", intervention_id=req.intervention_id, waited_s=self.timeout_s)
            self.machine.resume_complete()
            return InterventionResult(req.intervention_id, str(path), "abandoned", human_actions)
        self.log.event("human_resume_signal", intervention_id=req.intervention_id, decision=signal["decision"], note=signal.get("note", ""),
                       human_actions=len(human_actions))
        self.machine.resume_complete()
        return InterventionResult(req.intervention_id, str(path), signal["decision"], human_actions)
