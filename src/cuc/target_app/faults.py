"""Fault injection for the target app. TEST HARNESS ONLY.

Two mechanisms:
  * ``?fault=<kind>`` on any request applies the fault to that request.
  * ``/admin/fault/<kind>?route=<prefix>&count=<n>`` arms a process-wide fault
    that fires on the next ``count`` requests whose path starts with ``prefix``
    (default ``/members``). Armed faults let a replay hit an exceptional state
    without editing the artifact or the replay's inputs.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum


class FaultKind(StrEnum):
    NOT_FOUND = "not_found"            # member search returns "no member found"
    VALIDATION = "validation"          # sub-account form rejects input
    PERMISSION_DENIED = "permission_denied"
    SESSION_TIMEOUT = "session_timeout"
    INTERSTITIAL = "interstitial"      # HTML system-notice overlay with a Continue button
    CONFIRM_DIALOG = "confirm_dialog"  # JS confirm() on page load
    SLOW = "slow"                      # delayed response
    SERVER_ERROR = "server_error"      # HTTP 500 page


@dataclass
class ArmedFault:
    kind: FaultKind
    route_prefix: str
    remaining: int


class FaultRegistry:
    """Process-wide armed faults, consumed once per matching request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed: list[ArmedFault] = []

    def arm(self, kind: FaultKind, route_prefix: str = "/members", count: int = 1) -> ArmedFault:
        af = ArmedFault(kind, route_prefix, max(1, count))
        with self._lock:
            self._armed.append(af)
        return af

    def clear(self) -> None:
        with self._lock:
            self._armed.clear()

    def status(self) -> list[dict]:
        with self._lock:
            return [{"kind": a.kind, "route": a.route_prefix, "remaining": a.remaining} for a in self._armed]

    def consume(self, path: str) -> FaultKind | None:
        """Return the first armed fault matching ``path`` and decrement it."""
        with self._lock:
            for af in self._armed:
                if path.startswith(af.route_prefix):
                    af.remaining -= 1
                    if af.remaining <= 0:
                        self._armed.remove(af)
                    return af.kind
        return None
