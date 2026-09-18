"""Locator resolution over a Playwright page.

Order is fixed by the artifact: ROLE_NAME -> ANCHOR -> TEXT -> COORDINATES.
A strategy that yields more than one match is a failure for that strategy,
never a guess; the next strategy is tried and every attempt is reported.
"""
from __future__ import annotations

import uuid
from typing import Any

from playwright.sync_api import Frame, Page

from cuc.schema import Locator, LocatorStrategy

from .a11y import CUC_JS
from .base import LocatorError, Resolved


def frame_path(frame: Frame) -> str | None:
    names: list[str] = []
    f: Frame | None = frame
    while f is not None and f.parent_frame is not None:
        names.append(f.name or "?")
        f = f.parent_frame
    return "/".join(reversed(names)) or None


def find_frame(page: Page, path: str | None) -> Frame:
    if path is None:
        return page.main_frame
    for f in page.frames:
        if frame_path(f) == path:
            return f
    raise LocatorError(f"frame {path!r} not found; have {[frame_path(f) for f in page.frames]}", [])


def ensure_helpers(frame: Frame) -> None:
    frame.evaluate(CUC_JS)


def _try_one(page: Page, loc: Locator) -> tuple[Resolved | None, dict[str, Any]]:
    """Return (resolved, attempt_record). resolved is None when not exactly one match."""
    attempt: dict[str, Any] = {"strategy": loc.strategy.value, "frame": loc.frame}
    frame = find_frame(page, loc.frame)
    if loc.strategy is LocatorStrategy.ROLE_NAME:
        pl = frame.get_by_role(loc.role, name=loc.name, exact=loc.exact)  # type: ignore[arg-type]
        attempt.update(role=loc.role, name=loc.name)
    elif loc.strategy is LocatorStrategy.ANCHOR:
        ensure_helpers(frame)
        token = uuid.uuid4().hex
        n = frame.evaluate("([r, a, t]) => window.__cuc.markByAnchor(r, a, t)", [loc.role, loc.anchor_text, token])
        pl = frame.locator(f'[data-cuc-match="{token}"]')
        attempt.update(role=loc.role, anchor=loc.anchor_text, marked=n)
    elif loc.strategy is LocatorStrategy.TEXT:
        pl = frame.get_by_text(loc.text, exact=loc.exact)  # type: ignore[arg-type]
        attempt.update(text=loc.text)
    else:  # COORDINATES: translate frame-relative point to page coordinates
        offset = (0.0, 0.0)
        fe = frame.frame_element() if frame.parent_frame is not None else None
        if fe is not None:
            box = fe.bounding_box() or {"x": 0, "y": 0}
            offset = (box["x"], box["y"])
        vp = page.viewport_size or {"width": 0, "height": 0}
        attempt.update(x=loc.x, y=loc.y, recorded_viewport=list(loc.viewport or ()), viewport=[vp["width"], vp["height"]])
        if loc.viewport and (vp["width"], vp["height"]) != tuple(loc.viewport):
            attempt["matches"] = 0
            attempt["reason"] = "viewport differs from recording; coordinates untrusted"
            return None, attempt
        attempt["matches"] = 1
        return Resolved(loc.strategy, loc.frame, f"point({loc.x},{loc.y}) in {loc.frame or 'top'}",
                        point=(offset[0] + float(loc.x), offset[1] + float(loc.y))), attempt
    count = pl.count()
    attempt["matches"] = count
    if count != 1:
        if loc.strategy is LocatorStrategy.ANCHOR:
            frame.evaluate("() => window.__cuc.unmark()")
        return None, attempt
    desc = f"{loc.strategy.value}:{loc.role or ''}:{loc.name or loc.anchor_text or loc.text}"
    return Resolved(loc.strategy, loc.frame, desc, handle=pl), attempt


def resolve(page: Page, locators: list[Locator]) -> tuple[Resolved, list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    for loc in locators:
        try:
            resolved, attempt = _try_one(page, loc)
        except LocatorError as e:
            attempts.append({"strategy": loc.strategy.value, "frame": loc.frame, "error": str(e)})
            continue
        attempts.append(attempt)
        if resolved is not None:
            return resolved, attempts
    summary = "; ".join(
        f"{a['strategy']}={a.get('matches', 'err')}" + (f" ({a['reason']})" if a.get("reason") else "") for a in attempts
    )
    raise LocatorError(f"no locator resolved to exactly one control: {summary}", attempts)


def release(page: Page, resolved: Resolved) -> None:
    """Remove temporary anchor markers after acting."""
    if resolved.strategy is LocatorStrategy.ANCHOR:
        try:
            find_frame(page, resolved.frame).evaluate("() => window.__cuc && window.__cuc.unmark()")
        except Exception:  # frame navigated away; markers are gone with the document
            pass
