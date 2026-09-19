"""auth-state.json: the usage service's standing rejection of an account's login.

`forget` assumes the caller already holds `Manager.locked()` -- `Registry.remove`
and `Launcher.after_login` call it inside their own single lock hold, so the
sequences that span concerns keep taking one lock exactly once.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from xswap.core.errors import SwapError
from xswap.core.fsutil import atomic_json
from xswap.core.types import AuthStateEntry
from xswap.core.usage import AUTH_FAILED_STATUS

if TYPE_CHECKING:
    from xswap.manager import Manager, _Hooks


class AuthState:
    def __init__(self, manager: Manager, hooks: _Hooks) -> None:
        self.manager = manager
        self.hooks = hooks

    def path(self) -> Path:
        return self.manager.root / "auth-state.json"

    def read(self) -> dict[str, AuthStateEntry]:
        """{name: {"failedAt", "reason", "identity"}} or {} when absent/unreadable/not an object."""
        try:
            data = json.loads(self.manager.auth_state_path().read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def failure(self, name: str, label: str) -> AuthStateEntry | None:
        """{"failedAt", "reason"} when the usage service rejected NAME's *current* login and
        nothing has cleared it since, else None. Like cached_usage, an entry recorded under
        another login label (a re-login under the same name) is a miss, not reused.
        Read-only: doctor and offline views call this without taking the lock.
        """
        entry = self.manager._read_auth_state().get(name)
        if not isinstance(entry, dict) or entry.get("identity") != label:
            return None
        failed_at = entry.get("failedAt")
        if not isinstance(failed_at, (int, float)) or isinstance(failed_at, bool):
            return None
        return {"failedAt": failed_at, "reason": str(entry.get("reason") or AUTH_FAILED_STATUS), "identity": label}

    def remember(
        self, name: str, label: str, reason: str = AUTH_FAILED_STATUS, failed_at: float | None = None
    ) -> None:
        """Record a service-rejected login for NAME's current label. Keeps the first failedAt
        while the same login keeps failing, so doctor's "since" is the first rejection and a
        30-minute alert job does not rewrite the file. Labels only (may be an email); never
        tokens. Always 0600 through atomic_json.
        """
        with self.manager.locked():
            if not self.manager._still_registered(name):
                return
            data = self.manager._read_auth_state()
            current = data.get(name)
            if (isinstance(current, dict) and current.get("identity") == label and current.get("reason") == reason
                    and isinstance(current.get("failedAt"), (int, float)) and not isinstance(current.get("failedAt"), bool)):
                return
            data[name] = {"failedAt": time.time() if failed_at is None else failed_at, "reason": reason, "identity": label}
            atomic_json(self.manager.auth_state_path(), data)

    def forget(self, name: str) -> None:
        """Drop NAME's entry whatever its label. Caller holds the lock (mirrors UsageCache.forget)."""
        data = self.manager._read_auth_state()
        if data.pop(name, None) is not None:
            atomic_json(self.manager.auth_state_path(), data)

    def clear(self, name: str) -> None:
        """A successful fetch clears the record; no lock and no write when there is none."""
        if name not in self.manager._read_auth_state():
            return
        with self.manager.locked():
            self.manager._forget_auth_failure(name)


def is_auth_failed(manager: Manager, name: str) -> bool:
    """True while the usage service's rejection of NAME's current login still stands.

    Module-level so callers and tests can reach it without a Manager method lookup;
    the state itself lives in Manager.auth_failure (auth-state.json). The identity
    label goes through `manager._hooks`, which resolves `identity` in `xswap.manager`'s
    namespace at call time, so a patch there still bites.
    """
    try:
        _, home = manager.account(name)
        return manager.auth_failure(name, manager._hooks.identity(home)) is not None
    except SwapError:
        return False
