"""Observe -> decide -> act loop for discovery.

Every model-proposed action passes policy before execution. Stops: goal met, max steps,
timeout, dead end (same action on the same observed state three times), escalation.
Produces a trace; the recorder turns it into an artifact.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cuc.observability import RunLog, capture
from cuc.policy import Policy, SecretRef, SecretStore
from cuc.replay.executor import EscalationHandler, InterventionContext
from cuc.schema import ActionType, ExtractSpec, ExtractStrategy, Locator, LocatorStrategy, RiskClass
from cuc.surface.base import Control, LocatorError, Observation, Surface, SurfaceError
from cuc.surface.playwright_surface import parse_value

from .llm import LLMClient, LLMError, ToolCall, ToolResult
from .tools import TOOL_NAMES, TOOLS

_SECRET_PH = re.compile(r"\{\{secret:([A-Z][A-Z0-9_]+)\}\}")

SYSTEM_PROMPT = """You operate a legacy back-office web application on behalf of a bank operator, through a fixed set of tools.
You see the accessibility tree of every frame plus a list of interactive controls. You do not see pixels.

Rules:
1. One tool call per turn. After each action you receive the new observation.
2. Target controls by frame + role + name exactly as listed. If a control has no name, use its anchor text instead. Never guess.
3. Type credentials only as the exact placeholders listed under "Available secret placeholders" in the first message; the system substitutes the real value. Never invent credentials or placeholder names.
4. When you type a value that came from the goal and would change per invocation (an id, an amount), pass param_name so it becomes a typed input.
5. Do not click anything that commits, submits, transfers, deletes or opens an account unless the goal requires it; such actions require human confirmation and may be refused.
6. Use extract to read the values the goal asks for, then call done with a capability_id and description. If you are stuck, or the app shows an error you cannot resolve, call escalate.
7. Stay on the application's own screens. Navigate only to URLs on the allowlisted origin.
"""


@dataclass
class TraceEntry:
    index: int
    tool: str
    args: dict[str, Any]
    reason: str
    frame: str | None = None
    control: Control | None = None
    strategy: str | None = None
    url_before: str = ""
    url_after: str = ""
    hash_before: str = ""
    hash_after: str = ""
    text_before: dict[str, str] = field(default_factory=dict)
    text_after: dict[str, str] = field(default_factory=dict)
    human_confirmed: bool = False
    risk: RiskClass = RiskClass.REVERSIBLE
    error: str | None = None
    value: Any = None


@dataclass
class DiscoveryOutcome:
    status: str  # goal_met | max_steps | timeout | dead_end | escalated | error
    trace: list[TraceEntry]
    steps_used: int
    summary: str = ""
    capability_id: str | None = None
    description: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    dialog_accept_patterns: list[str] = field(default_factory=list)
    viewport: tuple[int, int] = (1280, 800)
    usage: dict[str, int] = field(default_factory=dict)


class AgentLoop:
    def __init__(self, surface: Surface, llm: LLMClient, policy: Policy, log: RunLog, secrets: SecretStore, run_dir: Path,
                 secret_refs: dict[str, str] | None = None, max_steps: int = 20, timeout_s: float = 600,
                 escalation: EscalationHandler | None = None, screenshots: bool = True, viewport: tuple[int, int] = (1280, 800)):
        self.surface, self.llm, self.policy, self.log, self.secrets = surface, llm, policy, log, secrets
        self.run_dir, self.secret_refs = run_dir, secret_refs or {}
        self.max_steps, self.timeout_s, self.escalation, self.screenshots, self.viewport = max_steps, timeout_s, escalation, screenshots, viewport
        self.trace: list[TraceEntry] = []
        self.outputs: dict[str, Any] = {}
        self.accept_patterns: list[str] = []
        self._seen: dict[str, int] = {}
        self._usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    # ------------------------------------------------------------------ run
    def run(self, goal: str, target_url: str) -> DiscoveryOutcome:
        started = time.monotonic()
        d = self.policy.check_url(target_url)
        if not d.allowed:
            return self._finish("error", f"target url rejected by policy: {d.reason}")
        self.log.event("discovery_started", goal=goal, target=target_url, model=f"{self.llm.provider}:{self.llm.model}", max_steps=self.max_steps)
        self.llm.start(SYSTEM_PROMPT, TOOLS)
        self.surface.act(ActionType.NAVIGATE, None, target_url, 15_000)
        # The entry navigation is part of the recorded flow.
        self._record_entry(target_url)
        obs = self.surface.observe()
        secrets_text = "".join(f"\n- {{{{secret:{n}}}}}: {desc}" for n, desc in self.secret_refs.items()) or "\n(none)"
        turn = self._call(lambda: self.llm.send_user(
            f"GOAL: {goal}\nTARGET: {target_url}\nAvailable secret placeholders:{secrets_text}\n\nCurrent observation:\n{obs.to_prompt(max_chars_per_frame=3500)}"))
        steps = 0
        while True:
            if turn is None:
                return self._finish("error", "model call failed")
            if steps >= self.max_steps:
                return self._finish("max_steps", f"stopped after {steps} steps")
            if time.monotonic() - started > self.timeout_s:
                return self._finish("timeout", f"stopped after {self.timeout_s:.0f}s")
            if not turn.tool_calls:
                steps += 1
                self.log.event("agent_no_action", text=turn.text[:500])
                turn = self._call(lambda: self.llm.send_user("You must call exactly one tool. Call done if the goal is met, escalate if you are stuck."))
                continue
            call, extra = turn.tool_calls[0], turn.tool_calls[1:]
            steps += 1
            result, stop = self._execute(call, steps, goal)
            if stop is not None:
                return stop
            results = [result] + [ToolResult(c.id, c.name, "ignored: one tool call per turn", True) for c in extra]
            turn = self._call(lambda: self.llm.send_tool_results(results))

    def _call(self, fn):
        try:
            t = fn()
        except LLMError as e:
            self.log.event("llm_error", error=str(e))
            return None
        for k, v in t.usage.items():
            self._usage[k] = self._usage.get(k, 0) + v
        self.log.event("llm_call", elapsed_s=t.elapsed_s, attempts=t.attempts, usage=t.usage, tool_calls=[c.name for c in t.tool_calls])
        if t.text:
            self.log.event("agent_thought", text=t.text[:1000])
        return t

    # -------------------------------------------------------------- execute
    def _execute(self, call: ToolCall, step_no: int, goal: str) -> tuple[ToolResult, DiscoveryOutcome | None]:
        name, args = call.name, dict(call.args)
        reason = str(args.get("reason", ""))
        self.log.event("agent_decision", step=step_no, tool=name, args=args, reason=reason)
        if name not in TOOL_NAMES:
            return ToolResult(call.id, name, f"unknown tool {name}", True), None
        if name == "done":
            if not any(t.tool in ("click", "type_text", "select_option", "press_key", "extract") for t in self.trace):
                return ToolResult(call.id, name, "nothing has been done yet; act first", True), None
            return ToolResult(call.id, name, "ok"), self._finish("goal_met", str(args.get("summary", "")),
                                                                   capability_id=str(args.get("capability_id", "")), description=str(args.get("description", "")))
        if name == "escalate":
            return self._escalate_from_model(call, str(args.get("reason", "")))
        if name == "handle_dialog":
            if args.get("action") == "accept":
                self.accept_patterns.append(str(args["pattern"]))
                self.surface.set_dialog_policy(self.accept_patterns)
            self.trace.append(TraceEntry(step_no, name, args, reason))
            return ToolResult(call.id, name, "dialog policy updated"), None

        before = self.surface.observe()
        entry = TraceEntry(step_no, name, args, reason, url_before=before.url, hash_before=before.state_hash(),
                           text_before={f.path or "top": f.text for f in before.frames})
        key = f"{entry.hash_before}|{name}|{json.dumps(args, sort_keys=True)}"
        self._seen[key] = self._seen.get(key, 0) + 1
        if self._seen[key] >= 3:
            self.trace.append(entry)
            return ToolResult(call.id, name, "dead end", True), self._finish("dead_end", f"repeated {name} on an unchanged state 3 times")
        try:
            msg = self._act(name, args, entry)
        except (LocatorError, SurfaceError, KeyError, ValueError) as e:
            entry.error = str(e)
            self.trace.append(entry)
            self.log.event("agent_action_failed", step=step_no, tool=name, error=str(e))
            return ToolResult(call.id, name, f"error: {e}", True), None
        except _Refused as e:
            entry.error = str(e)
            self.trace.append(entry)
            self.log.event("agent_action_refused", step=step_no, tool=name, reason=str(e))
            return ToolResult(call.id, name, f"refused: {e}", True), None
        after = self._settle(entry.hash_before, quiet_ms=300 if name in ("type_text", "select_option") else 800)
        entry.url_after, entry.hash_after = after.url, after.state_hash()
        entry.text_after = {f.path or "top": f.text for f in after.frames}
        self.trace.append(entry)
        if self.screenshots:
            try:
                self.surface.screenshot(self.run_dir / "evidence" / f"step_{step_no:02d}_{name}.png")
            except SurfaceError:
                pass
        self.log.event("agent_step", step=step_no, tool=name, strategy=entry.strategy, url=after.url, state=entry.hash_after, result=msg)
        return ToolResult(call.id, name, f"{msg}\n\nObservation:\n{after.to_prompt(max_chars_per_frame=3500)}"), None

    def _settle(self, before_hash: str, quiet_ms: int, max_ms: int = 4000) -> Observation:
        """Observe until the state stops changing: a changed state must hold for two samples, an unchanged one for quiet_ms."""
        start = time.monotonic()
        last = self.surface.observe()
        last_hash = last.state_hash()
        while (time.monotonic() - start) * 1000 < max_ms:
            self.surface.idle(150)
            cur = self.surface.observe()
            h = cur.state_hash()
            if h == last_hash and (h != before_hash or (time.monotonic() - start) * 1000 >= quiet_ms):
                return cur
            last, last_hash = cur, h
        return last

    def _act(self, name: str, args: dict[str, Any], entry: TraceEntry) -> str:
        url = self.surface.current_url()
        if name == "navigate":
            d = self.policy.check_action(ActionType.NAVIGATE, str(args["url"]))
            if not d.allowed:
                raise _Refused(d.reason)
            self.surface.act(ActionType.NAVIGATE, None, str(args["url"]), 15_000)
            return f"navigated to {args['url']}"
        if name == "extract":
            d = self.policy.check_action(ActionType.EXTRACT, url)
            if not d.allowed:
                raise _Refused(d.reason)
            spec = ExtractSpec(output=str(args["output_name"]), strategy=ExtractStrategy(args["strategy"]), frame=_frame(args.get("frame")),
                               row_anchor=args.get("row_anchor"), column_header=args.get("column_header"), label=args.get("label"),
                               parse=args.get("value_type", "string"))
            raw = self.surface.extract(spec, 5000)
            value = parse_value(raw, spec.parse)
            self.outputs[spec.output] = value
            entry.frame, entry.value, entry.risk = spec.frame, value, RiskClass.READ
            return f"extracted {spec.output} = {value!r} (raw {raw!r})"
        action = {"click": ActionType.CLICK, "type_text": ActionType.TYPE, "select_option": ActionType.SELECT, "press_key": ActionType.PRESS}[name]
        d = self.policy.check_action(action, url)
        if not d.allowed:
            raise _Refused(d.reason)
        frame = _frame(args.get("frame"))
        locators = _locators_from_args(frame, str(args["role"]), args.get("name"), args.get("anchor"))
        resolved = self.surface.resolve(locators, 5000)
        control = _find_control(self.surface.observe(), frame, str(args["role"]), args.get("name"), args.get("anchor"))
        entry.frame, entry.control, entry.strategy = frame, control, resolved.strategy.value
        entry.risk = self.policy.classify(action, str(args["role"]), args.get("name"), None)
        g = self.policy.gate(entry.risk)
        if not g.allowed:
            if not g.requires_human or self.escalation is None:
                raise _Refused(g.reason)
            res = self.escalation.request(self._ctx(entry, g.reason, "confirm_irreversible"))
            self.log.event("escalation_resolved", step=entry.index, decision=res.decision, intervention_id=res.intervention_id)
            if res.decision != "confirmed":
                raise _Refused("irreversible action was not confirmed by the human operator")
            entry.human_confirmed = True
        value: str | None = None
        is_secret = False
        if name == "type_text":
            value, is_secret = self._substitute(str(args["text"]))
        elif name == "select_option":
            value = str(args["option"])
        elif name == "press_key":
            value = str(args["key"])
        self.surface.act(action, resolved, value, 5000)
        shown = "[secret]" if is_secret else value
        return f"{name} on {resolved.description} ok" + (f" (value {shown!r})" if value is not None else "")

    def _substitute(self, text: str) -> tuple[str, bool]:
        m = _SECRET_PH.fullmatch(text.strip())
        if not m:
            if _SECRET_PH.search(text):
                raise ValueError("secret placeholders must be typed alone, not embedded in other text")
            return text, False
        env = m.group(1)
        if env not in self.secret_refs:
            raise ValueError(f"secret {env} is not available to this run; available placeholders: "
                             + ", ".join("{{secret:" + n + "}}" for n in self.secret_refs))
        return self.secrets.resolve(SecretRef(env)).reveal(), True

    # ----------------------------------------------------------- escalation
    def _escalate_from_model(self, call: ToolCall, reason: str) -> tuple[ToolResult, DiscoveryOutcome | None]:
        entry = TraceEntry(len(self.trace) + 1, "escalate", {"reason": reason}, reason)
        if self.escalation is None:
            self.trace.append(entry)
            return ToolResult(call.id, "escalate", "no operator available"), self._finish("escalated", reason)
        res = self.escalation.request(self._ctx(entry, reason, "stuck"))
        self.log.event("escalation_resolved", step=entry.index, decision=res.decision, intervention_id=res.intervention_id, human_actions=len(res.human_actions))
        if res.decision != "resumed":
            self.trace.append(entry)
            return ToolResult(call.id, "escalate", "abandoned"), self._finish("escalated", reason)
        obs = self.surface.observe()
        return ToolResult(call.id, "escalate", f"A human operator intervened and handed control back. Continue.\n\nObservation:\n{obs.to_prompt(max_chars_per_frame=3500)}"), None

    def _ctx(self, entry: TraceEntry, reason: str, kind: str) -> InterventionContext:
        ev = capture(self.surface, self.run_dir, f"discovery_{kind}", self.log.redactor, self.log.path)
        return InterventionContext(run_id=self.log.run_id, run_dir=self.run_dir, capability_id="(discovery)", description=reason,
                                   step_id=f"step{entry.index}", step_label=f"{entry.tool} {json.dumps(entry.args)[:80]}", reason=reason,
                                   kind=kind, evidence=ev, current_url=self.surface.current_url(), steps_completed=len(self.trace))

    # -------------------------------------------------------------- helpers
    def _record_entry(self, url: str) -> None:
        obs = self.surface.observe()
        self.trace.append(TraceEntry(0, "navigate", {"url": url, "reason": "open the application entry point"}, "entry point",
                                     url_after=obs.url, hash_after=obs.state_hash(), text_after={f.path or "top": f.text for f in obs.frames}))

    def _finish(self, status: str, summary: str, capability_id: str | None = None, description: str | None = None) -> DiscoveryOutcome:
        self.log.event("discovery_finished", status=status, summary=summary, steps=len(self.trace), outputs=self.outputs, usage=self._usage)
        return DiscoveryOutcome(status=status, trace=self.trace, steps_used=len(self.trace), summary=summary, capability_id=capability_id,
                                description=description, outputs=dict(self.outputs), dialog_accept_patterns=list(self.accept_patterns),
                                viewport=self.viewport, usage=dict(self._usage))


class _Refused(Exception):
    """Policy refused the action; reported to the model, never executed."""


def _frame(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "top", "(top)") else s


def _locators_from_args(frame: str | None, role: str, name: Any, anchor: Any) -> list[Locator]:
    out: list[Locator] = []
    if name:
        out.append(Locator(strategy=LocatorStrategy.ROLE_NAME, frame=frame, role=role, name=str(name), rationale="model-specified"))
    if anchor:
        out.append(Locator(strategy=LocatorStrategy.ANCHOR, frame=frame, role=role, anchor_text=str(anchor), rationale="model-specified"))
    if not out:
        raise ValueError("target needs a name or an anchor")
    return out


def _find_control(obs: Observation, frame: str | None, role: str, name: Any, anchor: Any) -> Control | None:
    for f in obs.frames:
        if f.path != frame:
            continue
        for c in f.controls:
            if c.role != role:
                continue
            if name and c.name == str(name):
                return c
            if anchor and not name and c.anchor.lower() == str(anchor).lower():
                return c
    return None
