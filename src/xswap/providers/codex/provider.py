"""`CodexProvider`: the one implementation of `xswap.providers.base.Provider` today.

Every method is a thin adapter. The behaviour still lives in the modules it
delegates to (`identity`, `credentials`, `usage`, `live`, `switch`, `launcher`,
`doctor`), so this file adds no logic of its own and changes nothing a user can
observe -- it only gives core one object to ask instead of a dozen imports it
was never allowed to have.

Every import is inside a method on purpose: `xswap.manager` imports
`xswap.providers`, and most of the modules below import `xswap.manager`, so an
import-time edge here would close that loop.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xswap.core.types import (
    AccountRecord,
    Check,
    CredentialState,
    Identity,
    UsageBucket,
    UsageSnapshot,
    UsageWindow,
)
from xswap.providers import base
from xswap.providers.codex.usage import CODEX_QUOTA

if TYPE_CHECKING:
    from xswap.manager import Manager

# Labels `identity()` returns that are not an address: a local state, not a login.
SENTINELS = ("not signed in", "unreadable auth cache")


class CodexProvider:
    """Codex CLI, the Codex/ChatGPT desktop app, and the OpenClaw logins beside them."""

    name = "codex"
    home_label = "Codex home"
    capabilities = frozenset({base.LIVE_SWITCH, base.DESKTOP_APP, base.PATH_WRAPPER,
                              base.PER_ACCOUNT_HOME, base.OPENCLAW_SYNC})
    quota_shape = CODEX_QUOTA

    # -- accounts --------------------------------------------------------------

    def identity(self, home: Path) -> Identity:
        from xswap.providers.codex.identity import identity
        label = identity(home)
        return Identity(label=label, stable_id=None if label in SENTINELS else label,
                        sentinel=label in SENTINELS)

    def credential_state(self, home: Path) -> CredentialState:
        from xswap.core.errors import CredentialError, SwapError
        from xswap.providers.codex.identity import check_file_store, identity
        try:
            check_file_store(home)
            label = identity(home)
        except (SwapError, CredentialError) as error:
            return CredentialState(CredentialState.UNREADABLE, str(error))
        if label == "not signed in":
            return CredentialState(CredentialState.SIGN_IN_REQUIRED, label)
        if label == "unreadable auth cache":
            return CredentialState(CredentialState.UNREADABLE, label)
        return CredentialState(CredentialState.OK)

    def account_home(self, root: str | Path, name: str) -> Path:
        """`<root>/profiles/<name>/codex`: where a managed Codex login lives."""
        from xswap.core.paths import profile_dir
        return profile_dir(root, name) / "codex"

    def prepare_home(self, manager: Manager, home: Path, source: Path, accounts: dict[str, AccountRecord]) -> None:
        from xswap.providers.codex.plugins import ensure_plugins
        from xswap.providers.codex.relocate import link_packages
        ensure_plugins(home, source)
        link_packages(manager, home, accounts)

    # -- quota -----------------------------------------------------------------

    def read_usage(
        self, manager: Manager, home: Path, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> UsageSnapshot:
        """One live quota read for HOME, normalized.

        `check_file_store` and `read_limits` go through `manager._hooks`, which
        resolves them in `xswap.manager`'s namespace at call time: that is what
        keeps `patch("xswap.manager.read_limits")` biting from here.
        """
        import time

        from xswap.core.errors import UsageError
        from xswap.providers.codex.usage import (
            normalize_limits,
            normalize_reset_credits,
        )
        manager._hooks.check_file_store(home)
        arguments = () if timeout is None else (timeout,)
        try:
            response = manager._hooks.read_limits(manager.codex(), env if env is not None else manager.env(home), *arguments)
        except OSError:
            # The one failure that is not the usage service's: no runnable binary.
            raise UsageError("cannot start Codex CLI") from None
        buckets = normalize_limits(response)
        return UsageSnapshot(buckets=buckets, reset_credits=normalize_reset_credits(response),
                             fetched_at=time.time(), shape=self.quota_shape,
                             windows=tuple(self._windows(buckets)),
                             extras={"spark": [b for b in buckets if b["id"] != self.quota_shape.bucket_id]})

    def _windows(self, buckets: list[UsageBucket]) -> Iterator[UsageWindow]:
        from xswap.core.quota import quota_windows
        from xswap.core.usage import window_label
        for window in quota_windows(buckets, self.quota_shape):
            yield UsageWindow(key=window_label(window), used_percent=window["usedPercent"],
                              remaining_percent=window["remainingPercent"],
                              window_minutes=window["windowMinutes"], resets_at=window["resetsAt"])

    # -- selection and launching ----------------------------------------------

    def activate(self, manager: Manager, name: str) -> None:
        """`xswap use NAME`: the registry selection plus the auto-pool reordering."""
        manager.use(name)

    def login(self, manager: Manager, name: str, **options: Any) -> int:
        return manager.login(name, **options)

    def launch(self, manager: Manager, name: str | None, argv: list[str], *, auto_pool: list[str] | None = None) -> int:
        """`xswap run`: one account, or the live bridge across AUTO_POOL."""
        if auto_pool:
            from xswap.providers.codex.codex_cli import launch_cli
            # launch_cli's `accounts` is the comma-joined form every other caller passes
            # (cli.commands.launch reads it straight from `--accounts`); this path had no
            # caller yet (INT-5614 dead code) so nothing observed it building a list instead.
            return launch_cli(manager, ','.join(auto_pool), list(argv))
        return manager.launch_cli(name, list(argv))

    def switch_running(self, manager: Manager, name: str) -> dict[str, Any]:
        from xswap.providers.codex.switch import switch_running
        return switch_running(manager, name)

    # -- presentation ----------------------------------------------------------

    # These three resolve through `codex_cli`, not `runs`, although `runs` is where
    # they are defined: that is the module the suite replaces them on
    # (`patch('xswap.codex_cli.status_data')`), and a function-local import there
    # keeps those patches biting -- the same late-lookup rule `wrapper._late` uses.

    def status_data(self, manager: Manager, cleanup: bool = True) -> dict[str, Any]:
        from xswap.providers.codex.codex_cli import status_data
        return status_data(manager, cleanup=cleanup)

    def bridge_hints(self, manager: Manager, state: dict[str, Any], version: str) -> list[dict[str, Any]]:
        from xswap.providers.codex.codex_cli import bridge_hints
        return bridge_hints(manager, state, version)

    def session_hints(self, manager: Manager, target_version: str) -> list[str]:
        from xswap.providers.codex.codex_cli import (
            bridge_hints,
            describe_bridge_hint,
            status_data,
        )
        return [describe_bridge_hint(hint)
                for hint in bridge_hints(manager, status_data(manager, cleanup=False), target_version)]

    def credit_lines(self, reset_credits: Any) -> list[str]:
        from xswap.providers.codex.usage import reset_credit_lines
        return reset_credit_lines(reset_credits)

    # -- diagnostics -----------------------------------------------------------

    def doctor_checks(self, manager: Manager) -> Iterable[Check]:
        from xswap.providers.codex.doctor import run
        return [Check.from_dict(result) for result in run(manager)]
