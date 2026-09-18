"""Secrets are passed by reference and resolved at the last moment.

A ``SecretRef`` names an environment variable. ``SecretStore.resolve`` returns the
value wrapped in ``Secret`` (masked repr) and registers it with the redactor so it
can never be written to a log, artifact or snapshot.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .redaction import Redactor

_REF = re.compile(r"^secret:([A-Z][A-Z0-9_]+)$")


@dataclass(frozen=True)
class SecretRef:
    env_name: str

    @classmethod
    def parse(cls, s: str) -> "SecretRef | None":
        m = _REF.match(s)
        return cls(m.group(1)) if m else None


class Secret:
    __slots__ = ("_v",)

    def __init__(self, v: str):
        self._v = v

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__


class SecretStore:
    def __init__(self, redactor: Redactor, env: dict[str, str] | None = None):
        self._env = env if env is not None else os.environ
        self._redactor = redactor

    def resolve(self, ref: SecretRef) -> Secret:
        v = self._env.get(ref.env_name)
        if v is None or v == "":
            raise KeyError(f"secret {ref.env_name} is not set in the environment")
        self._redactor.register_secret(v)
        return Secret(v)
