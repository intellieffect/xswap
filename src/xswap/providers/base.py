"""The contract a platform has to satisfy to be switchable by xswap.

`Provider` is a `typing.Protocol`, deliberately: an implementation satisfies it
by having the members, not by inheriting anything, so a provider module never
has to import this one and a test double is a plain object. Nothing here does
I/O and nothing here imports a provider.

Two rules keep the boundary honest:

* **Capabilities, never names.** Core asks `"live_switch" in provider.capabilities`,
  never `provider.name == "codex"`. A platform that gains or loses a surface
  changes one frozenset; every caller follows without being edited.
* **Neutral types only.** Everything crossing back into core is one of
  `xswap.core.types` (`Identity`, `CredentialState`, `UsageSnapshot`, `Check`,
  `QuotaShape`) or a plain string/int. Platform-specific detail rides in
  `UsageSnapshot.extras`, which core carries but never interprets.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from xswap.core.types import (  # noqa: F401  re-exported
    CAPABILITIES,
    DESKTOP_APP,
    LIVE_SWITCH,
    OPENCLAW_SYNC,
    PATH_WRAPPER,
    PER_ACCOUNT_HOME,
    AccountRecord,
    Check,
    CredentialState,
    Identity,
    QuotaShape,
    UsageSnapshot,
)

if TYPE_CHECKING:
    from xswap.manager import Manager



@runtime_checkable
class Provider(Protocol):
    """One AI coding platform, as far as account switching is concerned."""

    #: Registry key and the value stored in each account record's `"provider"`.
    name: str
    #: What this platform's user-visible home is called, for messages core composes.
    home_label: str
    #: Which surfaces this provider implements; see the constants above.
    capabilities: frozenset[str]
    #: Where this platform's subscription quota sits in a normalized snapshot.
    quota_shape: QuotaShape

    # -- accounts --------------------------------------------------------------

    def identity(self, home: Path) -> Identity:
        """The local, unverified label for the login in HOME."""
        ...

    def credential_state(self, home: Path) -> CredentialState:
        """Whether HOME's credentials can be used right now, and why not when they cannot."""
        ...

    def account_home(self, root: str | Path, name: str) -> Path:
        """The directory a managed account for NAME lives in under the state ROOT."""
        ...

    def prepare_home(self, manager: Manager, home: Path, source: Path, accounts: dict[str, AccountRecord]) -> None:
        """Make a freshly created managed HOME usable (shared tooling, never credentials)."""
        ...

    # -- quota -----------------------------------------------------------------

    def read_usage(
        self, manager: Manager, home: Path, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> UsageSnapshot:
        """Read HOME's quota windows. Raises this platform's usage error on failure."""
        ...

    # -- selection and launching ----------------------------------------------

    def activate(self, manager: Manager, name: str) -> None:
        """Make NAME the account new sessions get: registry selection plus any signalling."""
        ...

    def login(self, manager: Manager, name: str, **options: Any) -> Any:
        """Run this platform's interactive sign-in for NAME."""
        ...

    def launch(self, manager: Manager, name: str, argv: list[str], *, auto_pool: list[str] | None = None) -> int:
        """Run the platform's CLI as NAME (or across AUTO_POOL); returns an exit code."""
        ...

    def switch_running(self, manager: Manager, name: str) -> dict[str, Any]:
        """Move already-running sessions to NAME. Only with the `live_switch` capability."""
        ...

    # -- presentation ----------------------------------------------------------
    # What the neutral list/dashboard/upgrade views need but cannot know: which
    # sessions this platform has running, and the extra lines its `--details` view
    # shows. Core calls these; it never looks at what is inside them.

    def status_data(self, manager: Manager, cleanup: bool = True) -> dict[str, Any]:
        """This platform's running-session state, as the list view and doctor read it."""
        ...

    def bridge_hints(self, manager: Manager, state: dict[str, Any], version: str) -> list[dict[str, Any]]:
        """One hint per running session that is still on older code than VERSION."""
        ...

    def session_hints(self, manager: Manager, target_version: str) -> list[str]:
        """`bridge_hints`, already rendered to lines, for the upgrade notice."""
        ...

    def credit_lines(self, reset_credits: Any) -> list[str]:
        """Extra `--details` lines for this platform's reset credits; [] when it has none."""
        ...

    # -- diagnostics -----------------------------------------------------------

    def doctor_checks(self, manager: Manager) -> Iterable[Check]:
        """Read-only diagnostics for `xswap doctor`. Never mutates, never fetches."""
        ...
