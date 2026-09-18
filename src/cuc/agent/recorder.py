"""Turn a successful discovery trace into a capability artifact.

What is kept: ordered steps, locator lists with rationale, typed params (literal
values generalized), typed outputs, postconditions inferred from what appeared
on screen after each action, a final checkpoint, plus the product profile's
outcome detectors and recoveries. What is dropped: the model transcript,
observations, screenshots, and every secret value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from cuc.policy import Redactor
from cuc.schema import (
    ActionType, AppIdentity, Artifact, Checkpoint, Condition, ConditionKind, ExtractSpec, ExtractStrategy, InputParam,
    Locator, LocatorStrategy, OutcomeDetector, OutputField, Provenance, Recovery, RiskClass, Step, ValueRef,
)
from cuc.surface.base import Control

from .loop import DiscoveryOutcome, TraceEntry

_SECRET_PH = re.compile(r"^\{\{secret:([A-Z][A-Z0-9_]+)\}\}$")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


@dataclass
class AppProfile:
    vendor: str
    product: str
    version: str
    outcome_detectors: list[dict[str, Any]] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "AppProfile":
        raw = yaml.safe_load(path.read_text()) or {}
        return cls(vendor=str(raw["vendor"]), product=str(raw["product"]), version=str(raw["version"]),
                   outcome_detectors=list(raw.get("outcome_detectors", [])), recoveries=list(raw.get("recoveries", [])))


# ------------------------------------------------------------------ locators
def locators_for(control: Control, frame: str | None, viewport: tuple[int, int]) -> list[Locator]:
    out: list[Locator] = []
    if control.name:
        out.append(Locator(strategy=LocatorStrategy.ROLE_NAME, frame=frame, role=control.role, name=control.name, exact=True,
                           rationale=f"Accessible name {control.name!r} on a {control.role}: what an operator reads; survives layout and markup changes."))
    if control.anchor:
        out.append(Locator(strategy=LocatorStrategy.ANCHOR, frame=frame, role=control.role, anchor_text=control.anchor,
                           rationale=("Control has no accessible name; " if not control.name else "Fallback: ")
                           + f"the nearest preceding visible text {control.anchor!r} is the label cell, stable in table-based forms."))
    if control.name and control.role in ("link", "button"):
        out.append(Locator(strategy=LocatorStrategy.TEXT, frame=frame, text=control.name, exact=True,
                           rationale="Visible text fallback if role mapping differs on another version or surface."))
    x, y, w, h = control.bbox
    out.append(Locator(strategy=LocatorStrategy.COORDINATES, frame=frame, x=round(x + w / 2, 1), y=round(y + h / 2, 1), viewport=viewport,
                       rationale="Last resort; only valid at the recorded viewport and refused otherwise."))
    return out


# ----------------------------------------------------------------- pruning
def _target_key(t: TraceEntry) -> tuple:
    return (t.frame, t.args.get("role"), t.args.get("name"), t.args.get("anchor"))


def prune_trace(actions: list[TraceEntry], profile: AppProfile) -> list[TraceEntry]:
    """Keep the flow that actually reached the goal.

    1. A failed attempt ends where the page showed a declared terminal business outcome (e.g. SEC-401 after a
       sign-on with a missing field). Everything up to and including that action is dropped; the entry navigation
       (index 0) is always kept. The model recovered afterwards, so the recorded flow starts from the recovery.
    2. Consecutive type/select actions into the same control collapse to the last one (the last value wins).
    """
    terminal_texts = [d["condition"]["text"].lower() for d in profile.outcome_detectors
                      if d.get("condition", {}).get("kind", "text_present") == "text_present" and d["condition"].get("text")]
    cut = -1
    for i, t in enumerate(actions):
        if i == 0:
            continue
        after = " ".join(" ".join(v.split()) for v in t.text_after.values()).lower()
        if any(tx in after for tx in terminal_texts):
            cut = i
    kept = [actions[0]] + actions[cut + 1:] if cut >= 0 else list(actions)
    out: list[TraceEntry] = []
    for t in kept:
        if out and t.tool in ("type_text", "select_option") and out[-1].tool == t.tool and _target_key(out[-1]) == _target_key(t):
            out[-1] = t
        else:
            out.append(t)
    return out


# ------------------------------------------------------------ postconditions
_DATA_LIKE = re.compile(r"\d{4,}|\$\s?\d|^[\W\d]+$")


def distinctive_text(before: dict[str, str], after: dict[str, str], avoid: set[str], clean=None) -> tuple[str, str | None] | None:
    """The shortest short line that appeared in a frame after the action and was not there before.

    Heuristic, by design reviewable: headings ("Member Profile") are short and new; data rows and
    status bars are long or contain digits/values. Lines the redactor would alter are never used.
    """
    candidates: list[tuple[int, int, str, str | None]] = []
    order = 0
    for frame, text in after.items():
        prev = " ".join(before.get(frame, "").split()).lower()
        for line in text.splitlines():
            s = " ".join(line.split())
            order += 1
            # A tab means several cells on one row (label + value): data, not a heading.
            if "\t" in line or not (4 <= len(s) <= 60) or _DATA_LIKE.search(s) or s.lower() in prev:
                continue
            if any(a and a.lower() in s.lower() for a in avoid):
                continue
            if clean is not None and clean(s) != s:
                continue
            candidates.append((len(s), order, s, None if frame == "top" else frame))
    if not candidates:
        return None
    _, _, s, frame = min(candidates)
    return s, frame


def _text_present(text: str, frame: str | None, description: str) -> Condition:
    return Condition(kind=ConditionKind.TEXT_PRESENT, frame=frame, text=text, description=description)


# ------------------------------------------------------------------ recorder
def build_artifact(outcome: DiscoveryOutcome, goal: str, target_url: str, profile: AppProfile, run_id: str, model: str,
                   redactor: Redactor, version: str = "1.0.0", capability_id: str | None = None) -> Artifact:
    if outcome.status != "goal_met":
        raise ValueError(f"cannot record a run that ended with {outcome.status}")
    actions = [t for t in outcome.trace if t.error is None and t.tool in ("navigate", "click", "type_text", "select_option", "press_key", "extract")]
    actions = prune_trace(actions, profile)
    if not actions:
        raise ValueError("trace has no successful actions")

    params: dict[str, InputParam] = {}
    param_values: dict[str, str] = {}
    avoid: set[str] = set()
    steps: list[Step] = []
    outputs: list[OutputField] = []
    work_frames: dict[str, int] = {}

    for t in actions:
        if t.tool in ("type_text", "select_option"):
            raw = str(t.args.get("text", t.args.get("option", "")))
            pname = t.args.get("param_name")
            if pname and raw in goal and not _SECRET_PH.match(raw):
                pname = str(pname)
                if not re.match(r"^[a-z][a-z0-9_]{0,63}$", pname):
                    pname = re.sub(r"[^a-z0-9_]", "_", pname.lower()).strip("_") or "value"
                params.setdefault(pname, InputParam(name=pname, type="string", description=f"Value typed at '{t.args.get('anchor') or t.args.get('name')}' (from the goal).",
                                                    pattern=r"^\d+$" if raw.isdigit() else None, example=raw))
                param_values[pname] = raw
            avoid.add(raw)
    for t in actions:
        if t.frame:
            work_frames[t.frame] = work_frames.get(t.frame, 0) + 1
    work_frame = max(work_frames, key=work_frames.get) if work_frames else None

    for i, t in enumerate(actions):
        sid = f"s{i + 1:02d}"
        post = distinctive_text(t.text_before, t.text_after, avoid, clean=redactor.text)
        postcondition = _text_present(post[0], post[1], f"'{post[0]}' visible after {t.tool}") if post and t.tool != "type_text" else None
        if t.tool == "navigate":
            url = _templatize(str(t.args["url"]), param_values)
            steps.append(Step(id=sid, label="Open the application" if i == 0 else f"Navigate to {url}", action=ActionType.NAVIGATE,
                              value=ValueRef(kind="literal", value=url), risk_class=RiskClass.REVERSIBLE, postcondition=postcondition, notes=t.reason))
            continue
        if t.tool == "extract":
            a = t.args
            spec = ExtractSpec(output=str(a["output_name"]), strategy=ExtractStrategy(a["strategy"]), frame=t.frame, row_anchor=a.get("row_anchor"),
                               column_header=a.get("column_header"), label=a.get("label"), parse=a.get("value_type", "string"))
            outputs.append(OutputField(name=spec.output, type="number" if spec.parse in ("money", "number") else "integer" if spec.parse == "integer" else "string",
                                       format="money_usd" if spec.parse == "money" else None, description=t.reason or f"Extracted via {spec.strategy.value}"))
            steps.append(Step(id=sid, label=f"Read {spec.output}", action=ActionType.EXTRACT, risk_class=RiskClass.READ, extract=spec, notes=t.reason))
            continue
        if t.control is None:
            raise ValueError(f"trace step {t.index} ({t.tool}) has no resolved control; cannot record")
        locs = locators_for(t.control, t.frame, outcome.viewport)
        action = {"click": ActionType.CLICK, "type_text": ActionType.TYPE, "select_option": ActionType.SELECT, "press_key": ActionType.PRESS}[t.tool]
        value = _value_ref(t, param_values)
        label = _label(t)
        steps.append(Step(id=sid, label=label, action=action, locators=locs, value=value, risk_class=t.risk if not t.human_confirmed else RiskClass.IRREVERSIBLE,
                          postcondition=postcondition, notes=t.reason))

    # Final checkpoint: the last distinctive state observed. Fall back to the last step's postcondition chain.
    final = next((s.postcondition for s in reversed(steps) if s.postcondition is not None), None)
    last_id = steps[-1].id
    if final is None:
        final = Condition(kind=ConditionKind.URL_MATCHES, pattern=re.escape(actions[-1].url_after.split("?")[0]), description="final url")
    checkpoints = [Checkpoint(id="cp01", after_step=last_id, condition=final, description=f"Goal state reached: {final.description or final.text}")]

    detectors = [OutcomeDetector.model_validate(d) for d in profile.outcome_detectors]
    recoveries = [Recovery.model_validate(_resolve_profile_refs(r, steps[0].id, work_frame)) for r in profile.recoveries]
    for pat in outcome.dialog_accept_patterns:
        if not any(r.trigger.pattern == pat for r in recoveries):
            recoveries.append(Recovery(id=f"r{len(recoveries) + 1:02d}", description=f"Accept dialog matching {pat!r} (declared during discovery)",
                                       trigger=Condition(kind=ConditionKind.DIALOG_OPEN, pattern=pat), action="accept_dialog", max_attempts=3))

    cid = capability_id or outcome.capability_id or ""
    if not _SLUG.match(cid):
        cid = "capability_" + re.sub(r"[^a-z0-9]+", "_", run_id.lower()).strip("_")[:40]
    return Artifact(
        capability_id=cid, version=version,
        description=outcome.description or outcome.summary or goal,
        app=AppIdentity(vendor=profile.vendor, product=profile.product, version=profile.version, entry_url=target_url),
        inputs=list(params.values()), outputs=outputs, steps=steps, checkpoints=checkpoints,
        outcome_detectors=detectors, recoveries=recoveries,
        provenance=Provenance(recorded_at=datetime.now(timezone.utc), model=model, run_id=run_id, goal=redactor.text(goal), discovery_steps=len(actions)),
    )


def _value_ref(t: TraceEntry, param_values: dict[str, str]) -> ValueRef | None:
    if t.tool == "press_key":
        return ValueRef(kind="literal", value=str(t.args["key"]))
    if t.tool not in ("type_text", "select_option"):
        return None
    raw = str(t.args.get("text", t.args.get("option", "")))
    m = _SECRET_PH.match(raw.strip())
    if m:
        return ValueRef(kind="secret", name=m.group(1))
    for pname, pval in param_values.items():
        if raw == pval:
            return ValueRef(kind="param", name=pname)
    return ValueRef(kind="literal", value=_templatize(raw, param_values))


def _templatize(text: str, param_values: dict[str, str]) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    for pname, pval in sorted(param_values.items(), key=lambda kv: -len(kv[1])):
        if pval and pval in text:
            text = text.replace(pval, "{" + pname + "}")
    return text


def _label(t: TraceEntry) -> str:
    target = t.args.get("name") or t.args.get("anchor") or "control"
    verb = {"click": "Click", "type_text": "Enter value in", "select_option": "Select option in", "press_key": "Press key on"}[t.tool]
    return f"{verb} '{target}'"


def _resolve_profile_refs(raw: dict[str, Any], entry_step: str, work_frame: str | None) -> dict[str, Any]:
    r = dict(raw)
    if r.get("goto_step") == "$entry":
        r["goto_step"] = entry_step
    locs = []
    for l in r.get("locators", []):
        l = dict(l)
        if l.get("frame") == "$work":
            l["frame"] = work_frame
        locs.append(l)
    if locs:
        r["locators"] = locs
    return r
