"""Redaction at the logging/evidence boundary.

Two mechanisms: pattern rules for regulated data shapes (SSN, account/card numbers,
bearer tokens, key=value secrets) and registered literal secrets (values resolved from
secret references at runtime), so a credential can never reach a log or snapshot even
if it shows up inside page text.
"""
from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{secret:[A-Z][A-Z0-9_]+\}\}")
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("account", re.compile(r"\b\d{9,17}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]+")),
    ("kv_secret", re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*\S+")),
]


class Redactor:
    def __init__(self, sensitive_keys: set[str] | None = None):
        self.sensitive_keys = {k.lower() for k in (sensitive_keys or set())}
        self._secrets: list[str] = []

    def register_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)  # longest first so substrings do not leak

    def text(self, s: str) -> str:
        for v in self._secrets:
            s = s.replace(v, "[REDACTED:secret]")
        # {{secret:NAME}} placeholders are references, not values; keep them readable in logs.
        placeholders: list[str] = []
        s = _PLACEHOLDER.sub(lambda m: placeholders.append(m.group(0)) or f"\x00PH{len(placeholders) - 1}\x00", s)
        for name, pat in _PATTERNS:
            s = pat.sub(f"[REDACTED:{name}]", s)
        for i, ph in enumerate(placeholders):
            s = s.replace(f"\x00PH{i}\x00", ph)
        return s

    def value(self, key: str | None, v: Any) -> Any:
        if key is not None and key.lower() in self.sensitive_keys:
            return "[REDACTED]"
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {k: self.value(str(k), x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.value(None, x) for x in v]
        return v

    def __call__(self, obj: Any) -> Any:
        return self.value(None, obj)
