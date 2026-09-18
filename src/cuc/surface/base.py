"""The Surface protocol.

This is the seam between "how we perceive/act on a surface" and "the recorded flow".
The replay executor and the discovery agent only speak this protocol. A desktop
(UIA/AX) surface would implement the same methods over an OS accessibility tree
and OS-level input; the artifact schema does not change.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cuc.schema import ActionType, Condition, ExtractSpec, Locator, LocatorStrategy


class SurfaceError(Exception):
    """Surface could not perform the request (detached frame, closed page, timeout)."""


class LocatorError(SurfaceError):
    """No locator resolved unambiguously. ``attempts`` records what each strategy saw."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]):
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class Control:
    frame: str | None
    role: str
    name: str            # accessible name ('' when the app gives none)
    anchor: str          # nearest preceding visible text; the handle for unnamed controls
    value: str           # current value for inputs (masked for password fields)
    enabled: bool
    bbox: tuple[float, float, float, float]  # x, y, w, h in frame viewport pixels

    def describe(self) -> str:
        bits = [f"[{self.role}]"]
        if self.name:
            bits.append(f'name="{self.name}"')
        if self.anchor and self.anchor != self.name:
            bits.append(f'anchor="{self.anchor}"')
        if self.value:
            bits.append(f'value="{self.value}"')
        if not self.enabled:
            bits.append("(disabled)")
        return " ".join(bits)


@dataclass(frozen=True)
class DialogEvent:
    kind: str       # alert | confirm | prompt | beforeunload
    message: str
    handled: str    # accepted | dismissed


@dataclass
class FrameView:
    path: str | None
    url: str
    a11y: str                 # accessibility tree text (role/name), what a screen reader sees
    text: str                 # visible innerText
    controls: list[Control] = field(default_factory=list)


@dataclass
class Observation:
    url: str
    title: str
    frames: list[FrameView]
    dialogs: list[DialogEvent] = field(default_factory=list)

    def state_hash(self) -> str:
        h = hashlib.sha256()
        for f in self.frames:
            h.update((f.path or "").encode())
            h.update(f.url.split("?")[0].encode())
            h.update(" ".join(f.text.split()).encode())
        return h.hexdigest()[:16]

    def to_prompt(self, max_chars_per_frame: int = 6000) -> str:
        parts = [f"Page title: {self.title}", f"Top URL: {self.url}"]
        for d in self.dialogs:
            parts.append(f"! A {d.kind} dialog appeared and was {d.handled}: \"{d.message}\"")
        for f in self.frames:
            if not f.text.strip() and not f.controls:
                continue  # frameset containers carry nothing an operator can act on
            parts.append(f"\n### Frame: {f.path or '(top)'}  url={f.url}")
            a11y = f.a11y if len(f.a11y) <= max_chars_per_frame else f.a11y[:max_chars_per_frame] + "\n...(truncated)"
            parts.append("Accessibility tree:\n" + (a11y or "(empty)"))
            if f.controls:
                parts.append("Interactive controls (use role + name, or role + anchor when name is empty):")
                parts.extend("  " + c.describe() for c in f.controls)
        return "\n".join(parts)


@dataclass
class Resolved:
    strategy: LocatorStrategy
    frame: str | None
    description: str
    handle: Any = None                      # surface-specific (Playwright Locator)
    point: tuple[float, float] | None = None  # page coordinates for COORDINATES


@runtime_checkable
class Surface(Protocol):
    def observe(self) -> Observation: ...
    def resolve(self, locators: list[Locator], timeout_ms: int) -> Resolved: ...
    def act(self, action: ActionType, target: Resolved | None, value: str | None, timeout_ms: int) -> None: ...
    def check(self, condition: Condition) -> bool: ...
    def wait_for(self, condition: Condition, timeout_ms: int) -> bool: ...
    def wait_for_any(self, conditions: list[Condition], timeout_ms: int) -> int | None: ...
    def extract(self, spec: ExtractSpec, timeout_ms: int) -> str: ...
    def screenshot(self, path: Path) -> Path: ...
    def snapshot(self, path: Path) -> Path: ...
    def current_url(self) -> str: ...
    def history_back(self, timeout_ms: int) -> None: ...
    def idle(self, ms: int) -> None: ...  # yield to the surface event loop (poll interval), never a synchronization sleep
    def set_dialog_policy(self, accept_patterns: list[str]) -> None: ...
    def drain_dialogs(self) -> list[DialogEvent]: ...
    def close(self) -> None: ...
