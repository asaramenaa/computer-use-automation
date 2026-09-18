"""Playwright (Chromium) implementation of the Surface protocol.

Perception: per-frame accessibility tree (Chromium's ARIA snapshot) plus an
enumeration of interactive controls with role, accessible name and, for
unnamed controls, the nearest preceding visible text ("anchor"). Screenshots
are evidence, not perception.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Dialog, Error as PWError, Frame, Page, Playwright, sync_playwright

from cuc.schema import ActionType, Condition, ConditionKind, ExtractSpec, ExtractStrategy, Locator

from . import locators as L
from .a11y import CUC_JS
from .base import Control, DialogEvent, FrameView, LocatorError, Observation, Resolved, SurfaceError

POLL_MS = 100


def parse_value(raw: str, parse: str) -> str | int | float:
    if parse == "money":
        m = re.search(r"-?\$?\s*([\d,]+(?:\.\d+)?)", raw)
        if not m:
            raise SurfaceError(f"could not parse money from {raw!r}")
        v = float(m.group(1).replace(",", ""))
        return -v if raw.strip().startswith("-") or "(" in raw else v
    if parse == "integer":
        return int(re.sub(r"[^\d-]", "", raw))
    if parse == "number":
        return float(re.sub(r"[^\d.\-]", "", raw))
    return raw.strip()


class PlaywrightSurface:
    def __init__(self, headless: bool = True, viewport: tuple[int, int] = (1280, 800), slow_mo: int = 0):
        self._pw: Playwright = sync_playwright().start()
        self._browser: Browser = self._pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        self._ctx: BrowserContext = self._browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        self.page: Page = self._ctx.new_page()
        self._accept_patterns: list[re.Pattern[str]] = []
        self._dialogs: list[DialogEvent] = []
        self.page.on("dialog", self._on_dialog)

    # ------------------------------------------------------------ dialogs
    def _on_dialog(self, dialog: Dialog) -> None:
        accept = any(p.search(dialog.message) for p in self._accept_patterns)
        # Fail closed: an unknown dialog is dismissed, never accepted.
        self._dialogs.append(DialogEvent(dialog.type, dialog.message, "accepted" if accept else "dismissed"))
        try:
            dialog.accept() if accept else dialog.dismiss()
        except PWError:
            pass

    def set_dialog_policy(self, accept_patterns: list[str]) -> None:
        self._accept_patterns = [re.compile(p, re.I) for p in accept_patterns]

    def drain_dialogs(self) -> list[DialogEvent]:
        out, self._dialogs = self._dialogs, []
        return out

    def recent_dialogs(self) -> list[DialogEvent]:
        return list(self._dialogs)

    # ---------------------------------------------------------- perception
    def _frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def _frame(self, path: str | None) -> Frame:
        return L.find_frame(self.page, path)

    def _frame_text(self, frame: Frame) -> str:
        try:
            return frame.evaluate("() => document.body ? document.body.innerText : ''")
        except PWError:
            return ""

    def observe(self) -> Observation:
        views: list[FrameView] = []
        for frame in self._frames():
            path = L.frame_path(frame)
            if frame.url in ("", "about:blank"):
                continue
            try:
                a11y = frame.locator(":root").aria_snapshot(timeout=3000)
            except PWError:
                a11y = ""
            try:
                frame.evaluate(CUC_JS)
                raw = frame.evaluate("() => window.__cuc.controls()")
            except PWError:
                raw = []
            controls = [Control(path, c["role"], c["name"], c["anchor"], c["value"], c["enabled"], tuple(c["bbox"])) for c in raw]
            views.append(FrameView(path=path, url=frame.url, a11y=a11y, text=self._frame_text(frame), controls=controls))
        return Observation(url=self.page.url, title=self.page.title(), frames=views, dialogs=self.recent_dialogs())

    def current_url(self) -> str:
        return self.page.url

    def idle(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def history_back(self, timeout_ms: int) -> None:
        try:
            self.page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
        except PWError as e:
            raise SurfaceError(f"history back failed: {e.message.splitlines()[0]}") from e

    # -------------------------------------------------------------- acting
    def resolve(self, locators: list[Locator], timeout_ms: int) -> Resolved:
        deadline = time.monotonic() + timeout_ms / 1000
        last: LocatorError | None = None
        while True:
            try:
                resolved, _ = L.resolve(self.page, locators)
                return resolved
            except LocatorError as e:
                last = e
            except PWError as e:  # navigation in flight; keep polling
                last = LocatorError(f"surface busy: {e.__class__.__name__}", [])
            if time.monotonic() >= deadline:
                assert last is not None
                raise last
            self.page.wait_for_timeout(POLL_MS)

    def act(self, action: ActionType, target: Resolved | None, value: str | None, timeout_ms: int) -> None:
        try:
            if action is ActionType.NAVIGATE:
                if not value:
                    raise SurfaceError("navigate needs a url")
                self.page.goto(value, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            if target is None:
                raise SurfaceError(f"{action} needs a resolved target")
            if target.point is not None:
                if action is not ActionType.CLICK:
                    raise SurfaceError("coordinates support click only")
                self.page.mouse.click(*target.point)
                return
            h = target.handle
            if action is ActionType.CLICK:
                h.click(timeout=timeout_ms)
            elif action is ActionType.TYPE:
                h.fill(value or "", timeout=timeout_ms)
            elif action is ActionType.SELECT:
                try:
                    h.select_option(label=value, timeout=timeout_ms)
                except PWError:
                    h.select_option(value=value, timeout=timeout_ms)
            elif action is ActionType.PRESS:
                h.press(value or "Enter", timeout=timeout_ms)
            else:
                raise SurfaceError(f"unsupported action {action}")
        except PWError as e:
            raise SurfaceError(f"{action.value} failed: {e.message.splitlines()[0]}") from e
        finally:
            if target is not None:
                L.release(self.page, target)

    # ---------------------------------------------------------- conditions
    def check(self, condition: Condition) -> bool:
        k = condition.kind
        try:
            if k is ConditionKind.DIALOG_OPEN:
                pat = re.compile(condition.pattern or "", re.I)
                return any(pat.search(d.message) for d in self._dialogs)
            if k in (ConditionKind.TEXT_PRESENT, ConditionKind.TEXT_ABSENT):
                frames = [self._frame(condition.frame)] if condition.frame else self._frames()
                want = " ".join((condition.text or "").lower().split())
                present = any(want in " ".join(self._frame_text(f).lower().split()) for f in frames)
                return present if k is ConditionKind.TEXT_PRESENT else not present
            if k is ConditionKind.URL_MATCHES:
                url = self._frame(condition.frame).url if condition.frame else self.page.url
                return re.search(condition.pattern or "", url) is not None
            if k in (ConditionKind.ELEMENT_PRESENT, ConditionKind.ELEMENT_ABSENT):
                assert condition.locator is not None
                try:
                    resolved, attempts = L.resolve(self.page, [condition.locator])
                    L.release(self.page, resolved)
                    present = True
                except LocatorError as e:
                    present = any(a.get("matches", 0) > 0 for a in e.attempts)
                return present if k is ConditionKind.ELEMENT_PRESENT else not present
        except (LocatorError, PWError):
            return False
        raise SurfaceError(f"unknown condition {k}")

    def wait_for(self, condition: Condition, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if self.check(condition):
                return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(POLL_MS)

    def wait_for_any(self, conditions: list[Condition], timeout_ms: int) -> int | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for i, c in enumerate(conditions):
                if self.check(c):
                    return i
            if time.monotonic() >= deadline:
                return None
            self.page.wait_for_timeout(POLL_MS)

    # ----------------------------------------------------------- extraction
    def extract(self, spec: ExtractSpec, timeout_ms: int) -> str:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                hits = self._extract_once(spec)
            except PWError as e:
                hits = None
                err = str(e)
            else:
                err = ""
            if hits is not None and len(hits) == 1:
                return hits[0]
            if time.monotonic() >= deadline:
                if hits is None:
                    raise SurfaceError(f"extract {spec.output}: {err}")
                raise SurfaceError(f"extract {spec.output}: expected exactly one match, found {len(hits)}: {hits[:3]}")
            self.page.wait_for_timeout(POLL_MS)

    def _extract_once(self, spec: ExtractSpec) -> list[str]:
        s = spec.strategy
        if s is ExtractStrategy.URL_REGEX:
            url = self._frame(spec.frame).url if spec.frame else self.page.url
            m = re.search(spec.pattern or "", url)
            return [m.group(1)] if m else []
        if s is ExtractStrategy.ELEMENT_TEXT:
            assert spec.locator is not None
            try:
                resolved, _ = L.resolve(self.page, [spec.locator])
            except LocatorError:
                return []
            try:
                return [resolved.handle.inner_text()]
            finally:
                L.release(self.page, resolved)
        frame = self._frame(spec.frame)
        frame.evaluate(CUC_JS)
        if s is ExtractStrategy.TABLE_CELL:
            return frame.evaluate("([r, c]) => window.__cuc.tableCell(r, c)", [spec.row_anchor, spec.column_header])
        return frame.evaluate("(l) => window.__cuc.labelValue(l)", spec.label)

    # ------------------------------------------------------------ evidence
    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except PWError as e:
            raise SurfaceError(f"screenshot failed: {e}") from e
        return path

    def snapshot(self, path: Path) -> Path:
        """Accessibility snapshot of every frame, as text. The failure-time 'DOM' evidence."""
        path.parent.mkdir(parents=True, exist_ok=True)
        parts = [f"url: {self.page.url}", f"title: {self.page.title()}"]
        for d in self._dialogs:
            parts.append(f"dialog[{d.kind}] {d.handled}: {d.message}")
        for frame in self._frames():
            parts.append(f"\n=== frame: {L.frame_path(frame) or '(top)'}  url={frame.url}")
            try:
                parts.append(frame.locator(":root").aria_snapshot(timeout=3000))
            except PWError as e:
                parts.append(f"(unavailable: {e.__class__.__name__})")
        path.write_text("\n".join(parts))
        return path

    def close(self) -> None:
        for closer in (self._ctx.close, self._browser.close, self._pw.stop):
            try:
                closer()
            except Exception:
                pass
