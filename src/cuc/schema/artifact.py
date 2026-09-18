"""The capability artifact: a typed, versioned, reviewable description of a UI flow.

Design intent
-------------
* The artifact is a *contract* an agent can call (inputs/outputs), not a transcript.
* Every element the flow touches carries an ordered list of locators with a stated
  rationale. Replay tries them in order and refuses to guess on ambiguity.
* Runtime conditions are first-class: ``outcome_detectors`` name legitimate business
  outcomes, ``recoveries`` name known interstitials and retry policies. Anything else
  that breaks a postcondition is a hard failure.
* Nothing sensitive is ever serialized: typed values are references (param / secret /
  literal) and sensitive inputs are passed by reference.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class ActionType(StrEnum):
    NAVIGATE = "navigate"   # open a URL (value = url template)
    CLICK = "click"
    TYPE = "type"           # fill a text control
    SELECT = "select"       # choose an option in a select-like control
    PRESS = "press"         # keyboard key on a control (e.g. Enter)
    EXTRACT = "extract"     # read a value into an output


class RiskClass(StrEnum):
    READ = "read"                  # observes only
    REVERSIBLE = "reversible"      # navigation, typing into a form, opening a screen
    IRREVERSIBLE = "irreversible"  # commits state in the target system


class LocatorStrategy(StrEnum):
    ROLE_NAME = "role_name"      # accessibility role + accessible name (preferred)
    ANCHOR = "anchor"            # nearest control of a role after a text anchor (label cell)
    TEXT = "text"                # visible text match
    COORDINATES = "coordinates"  # last resort: recorded viewport point


class Locator(_Strict):
    strategy: LocatorStrategy
    frame: str | None = Field(default=None, description="Frame name path from the top document, e.g. 'main' or 'outer/inner'. None = top document.")
    role: str | None = Field(default=None, description="ARIA/accessibility role (button, link, textbox, combobox, cell...).")
    name: str | None = Field(default=None, description="Accessible name for ROLE_NAME.")
    exact: bool = True
    anchor_text: str | None = Field(default=None, description="ANCHOR: visible text (e.g. a label cell) the control sits after.")
    text: str | None = Field(default=None, description="TEXT: visible text of the control itself.")
    x: float | None = Field(default=None, description="COORDINATES: x in frame viewport pixels at recording.")
    y: float | None = None
    viewport: tuple[int, int] | None = Field(default=None, description="COORDINATES: viewport size at recording.")
    rationale: str = Field(min_length=1, description="Why this locator is believed robust, for reviewers.")

    @model_validator(mode="after")
    def _required_fields(self) -> "Locator":
        s = self.strategy
        if s is LocatorStrategy.ROLE_NAME and not (self.role and self.name):
            raise ValueError("role_name locator needs role and name")
        if s is LocatorStrategy.ANCHOR and not (self.role and self.anchor_text):
            raise ValueError("anchor locator needs role and anchor_text")
        if s is LocatorStrategy.TEXT and not self.text:
            raise ValueError("text locator needs text")
        if s is LocatorStrategy.COORDINATES and (self.x is None or self.y is None or self.viewport is None):
            raise ValueError("coordinates locator needs x, y and viewport")
        return self


class ValueRef(_Strict):
    """Where a typed/selected/navigated value comes from at replay time."""
    kind: Literal["literal", "param", "secret"]
    value: str | None = Field(default=None, description="literal: the constant; template form allows {param} placeholders.")
    name: str | None = Field(default=None, description="param: input name. secret: environment variable name, resolved at act time, never logged.")

    @model_validator(mode="after")
    def _shape(self) -> "ValueRef":
        if self.kind == "literal" and self.value is None:
            raise ValueError("literal ValueRef needs value")
        if self.kind == "param" and not self.name:
            raise ValueError("param ValueRef needs name")
        if self.kind == "secret" and not (self.name and _ENV_NAME.match(self.name)):
            raise ValueError("secret ValueRef needs an environment variable name")
        if self.kind == "secret" and self.value is not None:
            raise ValueError("secret ValueRef must not carry a value")
        return self


class ConditionKind(StrEnum):
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    URL_MATCHES = "url_matches"
    ELEMENT_PRESENT = "element_present"
    ELEMENT_ABSENT = "element_absent"
    DIALOG_OPEN = "dialog_open"  # a JS dialog whose message matches ``pattern`` is showing


class Condition(_Strict):
    kind: ConditionKind
    frame: str | None = None
    text: str | None = Field(default=None, description="Substring for TEXT_* (case-insensitive).")
    pattern: str | None = Field(default=None, description="Regex for URL_MATCHES / DIALOG_OPEN.")
    locator: Locator | None = Field(default=None, description="For ELEMENT_*.")
    description: str = ""

    @model_validator(mode="after")
    def _shape(self) -> "Condition":
        k = self.kind
        if k in (ConditionKind.TEXT_PRESENT, ConditionKind.TEXT_ABSENT) and not self.text:
            raise ValueError(f"{k} needs text")
        if k in (ConditionKind.URL_MATCHES, ConditionKind.DIALOG_OPEN):
            if not self.pattern:
                raise ValueError(f"{k} needs pattern")
            re.compile(self.pattern)
        if k in (ConditionKind.ELEMENT_PRESENT, ConditionKind.ELEMENT_ABSENT) and self.locator is None:
            raise ValueError(f"{k} needs locator")
        return self


class ExtractStrategy(StrEnum):
    TABLE_CELL = "table_cell"    # row containing row_anchor, column under column_header
    LABEL_VALUE = "label_value"  # cell immediately after the cell containing label
    ELEMENT_TEXT = "element_text"
    URL_REGEX = "url_regex"


class ExtractSpec(_Strict):
    output: str
    strategy: ExtractStrategy
    frame: str | None = None
    row_anchor: str | None = None
    column_header: str | None = None
    label: str | None = None
    locator: Locator | None = None
    pattern: str | None = Field(default=None, description="URL_REGEX: first capture group is the value.")
    parse: Literal["string", "money", "integer", "number"] = "string"

    @model_validator(mode="after")
    def _shape(self) -> "ExtractSpec":
        s = self.strategy
        if s is ExtractStrategy.TABLE_CELL and not (self.row_anchor and self.column_header):
            raise ValueError("table_cell needs row_anchor and column_header")
        if s is ExtractStrategy.LABEL_VALUE and not self.label:
            raise ValueError("label_value needs label")
        if s is ExtractStrategy.ELEMENT_TEXT and self.locator is None:
            raise ValueError("element_text needs locator")
        if s is ExtractStrategy.URL_REGEX:
            if not self.pattern:
                raise ValueError("url_regex needs pattern")
            if re.compile(self.pattern).groups < 1:
                raise ValueError("url_regex pattern needs one capture group")
        return self


class Step(_Strict):
    id: str = Field(pattern=r"^s\d{2,3}$")
    label: str = Field(min_length=1, description="Human-readable intent, e.g. 'Enter member number'.")
    action: ActionType
    locators: list[Locator] = Field(default_factory=list, description="Ordered by preference; replay stops at the first unambiguous hit.")
    value: ValueRef | None = None
    risk_class: RiskClass = RiskClass.REVERSIBLE
    precondition: Condition | None = Field(default=None, description="Must hold before acting; re-verified after a human handoff.")
    postcondition: Condition | None = Field(default=None, description="Must hold after acting, else hard failure (unless a detector claims the state).")
    timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    extract: ExtractSpec | None = None
    notes: str = Field(default="", description="Discovery-time reasoning kept for reviewers. Never the transcript.")

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        a = self.action
        if a in (ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.PRESS) and not self.locators:
            raise ValueError(f"{a} step {self.id} needs at least one locator")
        if a in (ActionType.TYPE, ActionType.SELECT, ActionType.NAVIGATE, ActionType.PRESS) and self.value is None:
            raise ValueError(f"{a} step {self.id} needs a value")
        if a is ActionType.EXTRACT and self.extract is None:
            raise ValueError(f"extract step {self.id} needs an extract spec")
        if a is ActionType.EXTRACT and self.risk_class is not RiskClass.READ:
            raise ValueError("extract steps are read-only")
        return self


class InputParam(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    required: bool = True
    sensitive: bool = Field(default=False, description="If true the caller passes 'secret:<ENV_NAME>' and the value is never logged or serialized.")
    pattern: str | None = Field(default=None, description="Regex the value must match before replay starts.")
    example: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "InputParam":
        if self.pattern:
            re.compile(self.pattern)
        if self.sensitive and self.example is not None:
            raise ValueError("sensitive inputs must not carry an example value")
        return self


class OutputField(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    format: str | None = Field(default=None, description="Hint for callers, e.g. 'money_usd'.")


class Checkpoint(_Strict):
    id: str = Field(pattern=r"^cp\d{2}$")
    after_step: str
    condition: Condition
    description: str = ""


class OutcomeDetector(_Strict):
    """A legitimate business result the caller must be told about. Terminal for the run."""
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    description: str
    condition: Condition
    after_steps: list[str] | None = Field(default=None, description="Only evaluated after these steps; None = after any step.")


class RecoveryAction(StrEnum):
    DISMISS = "dismiss"            # click the locators (known interstitial)
    ACCEPT_DIALOG = "accept_dialog"  # accept a JS dialog matching the trigger pattern
    RETRY = "retry"                # re-run the current step after backoff (transient load / 500)
    REAUTH = "reauth"              # jump back to goto_step (login) then continue from the failed step


class Recovery(_Strict):
    id: str = Field(pattern=r"^r\d{2}$")
    description: str
    trigger: Condition
    action: RecoveryAction
    locators: list[Locator] = Field(default_factory=list)
    goto_step: str | None = None
    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_ms: int = Field(default=1000, ge=0, le=30_000)

    @model_validator(mode="after")
    def _shape(self) -> "Recovery":
        if self.action is RecoveryAction.DISMISS and not self.locators:
            raise ValueError("dismiss recovery needs locators")
        if self.action is RecoveryAction.REAUTH and not self.goto_step:
            raise ValueError("reauth recovery needs goto_step")
        if self.action is RecoveryAction.ACCEPT_DIALOG and self.trigger.kind is not ConditionKind.DIALOG_OPEN:
            raise ValueError("accept_dialog recovery must trigger on dialog_open")
        return self


class AppIdentity(_Strict):
    vendor: str
    product: str
    version: str
    tenant: str | None = Field(default=None, description="None for a base artifact shared across tenants of this product.")
    entry_url: str = Field(description="Entry point. Origin may differ per tenant; replay validates it against policy.")


class Provenance(_Strict):
    recorded_at: datetime
    model: str
    run_id: str
    surface: str = "playwright-chromium"
    goal: str = Field(description="The natural-language goal, redacted.")
    discovery_steps: int = Field(ge=1)


class Artifact(_Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability_id: str = Field(pattern=_SLUG.pattern)
    version: str = Field(pattern=_SEMVER.pattern)
    description: str = Field(min_length=1)
    app: AppIdentity
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    checkpoints: list[Checkpoint] = Field(min_length=1)
    outcome_detectors: list[OutcomeDetector] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)
    provenance: Provenance

    # ------------------------------------------------------------ validation
    @model_validator(mode="after")
    def _cross_refs(self) -> "Artifact":
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique")
        params = {p.name for p in self.inputs}
        produced: set[str] = set()
        for s in self.steps:
            if s.value is not None:
                if s.value.kind == "param" and s.value.name not in params:
                    raise ValueError(f"step {s.id} references unknown param {s.value.name!r}")
                if s.value.kind == "literal":
                    for ph in re.findall(r"\{([a-z][a-z0-9_]*)\}", s.value.value or ""):
                        if ph not in params:
                            raise ValueError(f"step {s.id} template references unknown param {ph!r}")
            if s.extract is not None:
                produced.add(s.extract.output)
        declared = {o.name for o in self.outputs}
        if declared != produced:
            raise ValueError(f"outputs {sorted(declared)} must equal extracted values {sorted(produced)}")
        for cp in self.checkpoints:
            if cp.after_step not in step_ids:
                raise ValueError(f"checkpoint {cp.id} refers to unknown step {cp.after_step}")
        if not any(cp.after_step == step_ids[-1] for cp in self.checkpoints):
            raise ValueError("the last step must be covered by a checkpoint (the success condition)")
        for d in self.outcome_detectors:
            for sid in d.after_steps or []:
                if sid not in step_ids:
                    raise ValueError(f"detector {d.code} refers to unknown step {sid}")
        codes = [d.code for d in self.outcome_detectors]
        if len(set(codes)) != len(codes):
            raise ValueError("outcome detector codes must be unique")
        for r in self.recoveries:
            if r.goto_step and r.goto_step not in step_ids:
                raise ValueError(f"recovery {r.id} refers to unknown step {r.goto_step}")
        return self

    # ----------------------------------------------------------- contracts
    def input_json_schema(self) -> dict[str, Any]:
        """JSON Schema for the per-invocation inputs (what a calling agent supplies)."""
        props: dict[str, Any] = {}
        for p in self.inputs:
            prop: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.sensitive:
                prop["type"] = "string"
                prop["pattern"] = r"^secret:[A-Z][A-Z0-9_]+$"
                prop["description"] = (p.description + " Sensitive: pass as 'secret:<ENV_NAME>'.").strip()
            elif p.pattern:
                prop["pattern"] = p.pattern
            if p.example is not None:
                prop["examples"] = [p.example]
            props[p.name] = prop
        return {
            "type": "object",
            "properties": props,
            "required": [p.name for p in self.inputs if p.required],
            "additionalProperties": False,
        }

    def output_json_schema(self) -> dict[str, Any]:
        props = {o.name: {"type": o.type, "description": o.description, **({"format": o.format} if o.format else {})} for o in self.outputs}
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)

    def step_index(self, step_id: str) -> int:
        return next(i for i, s in enumerate(self.steps) if s.id == step_id)

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check caller-supplied params against the contract. Returns a normalized copy."""
        out: dict[str, Any] = {}
        unknown = set(params) - {p.name for p in self.inputs}
        if unknown:
            raise ValueError(f"unknown params: {sorted(unknown)}")
        for p in self.inputs:
            if p.name not in params:
                if p.required:
                    raise ValueError(f"missing required param {p.name!r}")
                continue
            v = params[p.name]
            if p.sensitive:
                if not (isinstance(v, str) and re.match(r"^secret:[A-Z][A-Z0-9_]+$", v)):
                    raise ValueError(f"sensitive param {p.name!r} must be passed as 'secret:<ENV_NAME>'")
            else:
                v = str(v)
                if p.pattern and not re.fullmatch(p.pattern, v):
                    raise ValueError(f"param {p.name!r} does not match {p.pattern!r}")
            out[p.name] = v
        return out
