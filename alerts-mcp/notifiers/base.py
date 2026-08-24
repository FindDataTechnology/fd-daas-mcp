"""Notifier ABC + shared types."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional


class Notifier(ABC):
    """Outbound channel adapter. Subclasses read their env keys at construction."""

    #: Channel name (matches the keys in a rule's channels_json).
    name: str = ""
    #: Env var names this adapter reads; `missing_keys()` reports which are unset.
    env_keys: tuple[str, ...] = ()

    def _env(self, key: str) -> Optional[str]:
        val = os.environ.get(key)
        return val if val and val.strip() else None

    def missing_keys(self) -> list[str]:
        return [k for k in self.env_keys if self._env(k) is None]

    def is_configured(self) -> bool:
        return not self.missing_keys()

    @abstractmethod
    def send(self, message: str, ctx: dict) -> dict:
        """Send `message` to this channel. Returns ``{"ok": bool, "error": str?}``.

        Never raise on transport error — return ``{"ok": False, "error": ...}``
        so a dispatch fan-out records the failure instead of aborting siblings.
        """
        ...

    def to_status(self) -> dict:
        return {
            "name": self.name,
            "configured": self.is_configured(),
            "missing_keys": self.missing_keys(),
        }
