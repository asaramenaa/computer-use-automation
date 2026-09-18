"""Configurable allowlist (origins, routes, action types) and risk gating. Fail closed."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from cuc.schema import ActionType, RiskClass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    requires_human: bool = False


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = "^"
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else re.escape(c)
        i += 1
    return re.compile(out + "$")


class Policy:
    def __init__(self, raw: dict):
        allow = raw.get("allow") or {}
        deny = raw.get("deny") or {}
        risk = raw.get("risk") or {}
        self.origins: set[str] = {o.rstrip("/").lower() for o in allow.get("origins", [])}
        self.allow_routes = [_glob_to_regex(p) for p in allow.get("routes", [])]
        self.deny_routes = [_glob_to_regex(p) for p in deny.get("routes", [])]
        self.actions: set[ActionType] = {ActionType(a) for a in allow.get("actions", [])}
        self.irreversible_mode: str = risk.get("irreversible_mode", "require_human")
        if self.irreversible_mode not in ("block", "require_human"):
            raise ValueError("risk.irreversible_mode must be block or require_human")
        self.irreversible_patterns = [re.compile(p) for p in risk.get("irreversible_control_patterns", [])]
        self.sensitive_keys: set[str] = {k.lower() for k in (raw.get("redaction") or {}).get("sensitive_keys", [])}

    @classmethod
    def load(cls, path: Path) -> "Policy":
        return cls(yaml.safe_load(path.read_text()) or {})

    # ------------------------------------------------------------ allowlist
    def check_url(self, url: str) -> Decision:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if origin not in self.origins:
            return Decision(False, f"origin {origin} is not allowlisted")
        path = parts.path or "/"
        if any(p.match(path) for p in self.deny_routes):
            return Decision(False, f"route {path} is explicitly denied")
        if not any(p.match(path) for p in self.allow_routes):
            return Decision(False, f"route {path} is not allowlisted")
        return Decision(True, "url allowed")

    def check_action(self, action: ActionType, url: str) -> Decision:
        if action not in self.actions:
            return Decision(False, f"action {action.value} is not allowlisted")
        return self.check_url(url)

    # ---------------------------------------------------------------- risk
    def classify(self, action: ActionType, control_role: str | None, control_name: str | None, declared: RiskClass | None = None) -> RiskClass:
        """Policy's view of a step's risk. The stricter of the declared class and the heuristic wins."""
        if action is ActionType.EXTRACT:
            heuristic = RiskClass.READ
        elif action in (ActionType.CLICK, ActionType.PRESS) and control_name and any(p.search(control_name) for p in self.irreversible_patterns):
            heuristic = RiskClass.IRREVERSIBLE
        else:
            heuristic = RiskClass.REVERSIBLE
        order = [RiskClass.READ, RiskClass.REVERSIBLE, RiskClass.IRREVERSIBLE]
        return max([heuristic] + ([declared] if declared else []), key=order.index)

    def gate(self, risk: RiskClass, human_confirmed: bool = False) -> Decision:
        if risk is not RiskClass.IRREVERSIBLE:
            return Decision(True, f"{risk.value} action")
        if self.irreversible_mode == "block":
            return Decision(False, "irreversible action blocked by policy (irreversible_mode=block)")
        if human_confirmed:
            return Decision(True, "irreversible action confirmed by human")
        return Decision(False, "irreversible action requires human confirmation", requires_human=True)
