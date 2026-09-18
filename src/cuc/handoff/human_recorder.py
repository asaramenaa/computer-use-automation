"""Capture what the human does in the live browser while they hold the lease.

Surface-specific by nature (a desktop surface would hook OS input). Records clicks
(role/name of the target), field changes (identity and value length only, never the
value), submits and frame navigations.
"""
from __future__ import annotations

from typing import Any, Callable

from playwright.sync_api import Error as PWError, Frame, Page

from cuc.surface.locators import frame_path

_LISTENER_JS = r"""
() => {
  if (window.__cucHuman) return;
  window.__cucHuman = true;
  const send = (payload) => { try { window.__cucHumanEvent(payload); } catch (e) {} };
  const describe = (el) => {
    const c = el.closest('a[href],button,input,select,textarea,[role=button],[role=link]') || el;
    const tag = c.tagName.toLowerCase();
    const type = (c.getAttribute('type') || '').toLowerCase();
    let role = tag === 'a' ? 'link' : tag === 'button' || ['submit','button','reset','image'].includes(type) ? 'button'
             : tag === 'select' ? 'combobox' : tag === 'input' || tag === 'textarea' ? (type === 'checkbox' ? 'checkbox' : type === 'radio' ? 'radio' : 'textbox') : (c.getAttribute('role') || 'generic');
    const name = (c.getAttribute('aria-label') || (['button'].includes(role) ? c.value : '') || (role === 'link' ? c.innerText : '') || '').trim().slice(0, 80);
    return { role, name, tag, type, fieldName: c.getAttribute('name') || '', text: (c.innerText || '').trim().slice(0, 80) };
  };
  document.addEventListener('click', (e) => send({ kind: 'click', target: describe(e.target) }), true);
  document.addEventListener('change', (e) => {
    const t = e.target; const type = (t.getAttribute('type') || '').toLowerCase();
    send({ kind: 'change', target: describe(t), value_length: type === 'password' ? null : String(t.value || '').length });
  }, true);
  document.addEventListener('submit', (e) => send({ kind: 'submit', target: { tag: 'form', action: (e.target.getAttribute('action') || '') } }), true);
}
"""


_ACTIVE: dict[int, "HumanActionRecorder"] = {}   # page id -> recorder currently capturing
_BOUND: set[int] = set()                           # pages whose binding/listener are already registered


class HumanActionRecorder:
    """One binding per page, registered once; whichever recorder is active receives the events."""

    def __init__(self, page: Page, on_event: Callable[[dict[str, Any]], None]):
        self.page = page
        self._on_event = on_event
        self.events: list[dict[str, Any]] = []

    def start(self) -> None:
        _ACTIVE[id(self.page)] = self
        if id(self.page) not in _BOUND:
            page = self.page
            page.expose_binding("__cucHumanEvent", lambda source, payload: _dispatch(page, source, payload))
            page.add_init_script(f"({_LISTENER_JS})()")
            page.on("framenavigated", lambda frame: _dispatch_nav(page, frame))
            _BOUND.add(id(page))
        for frame in self.page.frames:
            _inject(frame)

    def stop(self) -> list[dict[str, Any]]:
        if _ACTIVE.get(id(self.page)) is self:
            del _ACTIVE[id(self.page)]
        out, self.events = self.events, []
        return out

    def _receive(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        self._on_event(ev)


def _inject(frame: Frame) -> None:
    try:
        frame.evaluate(_LISTENER_JS)
    except PWError:
        pass


def _dispatch(page: Page, source: Any, payload: dict[str, Any]) -> None:
    rec = _ACTIVE.get(id(page))
    if rec is not None:
        rec._receive({"frame": frame_path(source["frame"]), **payload})


def _dispatch_nav(page: Page, frame: Frame) -> None:
    rec = _ACTIVE.get(id(page))
    if rec is not None:
        rec._receive({"kind": "navigated", "frame": frame_path(frame), "url": frame.url})
        _inject(frame)
