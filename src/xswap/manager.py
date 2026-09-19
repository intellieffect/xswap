#!/usr/bin/env python3
"""Account-scoped launchers for Codex CLI and the Codex/ChatGPT desktop app.

`Manager` is a facade. The state it owns is split across collaborators, one
module per concern -- `registry`, `mappings`, `usage_cache`, `auth_state`,
`openclaw_sync`, `launcher` -- plus the pure helpers in `ranking`, `identity`
and `reports`. None of them imports this module, and every one of them calls
back through the `Manager` it was given, so a patched `Manager` method still
takes effect wherever the work now lives.

Two rules hold the split together:

* Locking. There is exactly one lock file (`<root>/.lock`) and exactly one
  acquisition per read-modify-write, taken by whichever collaborator method the
  facade delegates to. Collaborators never take a lock of their own, and the
  helpers a sequence spanning two stores needs (`_forget_usage`,
  `_forget_auth_failure`, `_still_registered`) stay lock-free so `remove` and
  `after_login` keep one hold across both.
* Patchability. This module's globals (`read_limits`, `identity`,
  `check_file_store`, `private_dir`, ...) are what the test suite replaces.
  Collaborators reach them through `_Hooks`, whose methods resolve them here at
  call time, so `patch("xswap.manager.read_limits")` still bites. Names patched
  as *module attributes* (`xswap.manager.subprocess.run`, `.shutil.which`,
  `.sys.platform`, `.os.getcwd`) need no indirection -- those are the same
  module objects everywhere -- but the imports below must stay for the patch
  target to resolve.
"""
from __future__ import annotations

import math
import os
import shutil  # noqa: F401  patch target: xswap.manager.shutil.which
import subprocess  # noqa: F401  patch target: xswap.manager.subprocess.{run,call}
import sys
import time  # noqa: F401  patch target: xswap.manager.time
from pathlib import Path
from typing import Any

from xswap import fsutil, locking, providers
from xswap._version import (
    __version__,  # noqa: F401  re-exported: the codex_swap shim and `xswap --version`
)
from xswap.alert import AlertError  # noqa: F401  re-exported
from xswap.alert import install as alert_install  # noqa: F401  re-exported

# Re-exported so `doctor`, `init`, `tick`, the `codex_swap` shim and the tests keep
# importing these from `xswap.manager`, which is where they have always lived.
from xswap.auth_state import (  # noqa: F401  re-exported: `tick` and the suite import it here
    AuthState,
    is_auth_failed,
)
from xswap.core.quota import quota_windows
from xswap.core.types import (
    AccountRecord,
    AccountUsageRow,
    RegistryData,
    UsageBucket,
)
from xswap.credentials import CredentialError, read_auth  # noqa: F401
from xswap.display import resolve_lang  # noqa: F401  re-exported
from xswap.errors import SwapError
from xswap.fsutil import (
    atomic_json,  # noqa: F401  re-exported: tests and siblings import it from here
)
from xswap.identity import chatgpt_org_id, check_file_store, identity
from xswap.launcher import Launcher
from xswap.live import LiveError, buckets_available, jwt_claims  # noqa: F401
from xswap.mappings import Mappings
from xswap.openclaw_sync import OpenClawSync, parse_pool, resolve_openclaw_package_root
from xswap.path import absolute_which, relative_entry_message  # noqa: F401
from xswap.paths import (  # noqa: F401
    ROOT_VARIABLE,
    auto_dir,
    cli_runs_dir,
    default_root,
    profile_dir,
    settings_path,
)
from xswap.plugins import ensure_plugins  # noqa: F401
from xswap.ranking import rank_candidates, window_percent  # noqa: F401
from xswap.registry import UNSET as _UNSET
from xswap.registry import Registry
from xswap.relocate import link_packages  # noqa: F401
from xswap.reports import (  # noqa: F401
    describe_login_report,
    describe_switch_report,
    failure_reasons_text,
)
from xswap.upgrade import (  # noqa: F401  re-exported: xswap.cli.commands.maintenance reads `upgrade` here
    UpgradeError,
    upgrade,
)
from xswap.usage import (  # noqa: F401
    AUTH_FAILED_STATUS,
    CODEX_QUOTA,
    UsageError,
    is_ok,
    is_sign_in_failure,
    normalize_limits,
    normalize_reset_credits,
    read_limits,
    short_line,
    window_label,
)
from xswap.usage import (
    warnings as usage_warnings,  # noqa: F401  re-exported: xswap.cli.commands.usage reads it here
)
from xswap.usage_cache import UsageCache


def private_dir(path: Path) -> None:
    """fsutil.private_dir reporting through SwapError, this surface's error class.

    Kept as a function defined here (rather than a re-exported name) so it stays
    patchable on this module: `xswap.codex_cli` and `xswap.plugins` look it up
    through `xswap.manager` at call time.
    """
    return fsutil.private_dir(path, SwapError)


def validate_warn_threshold(value: Any) -> float:
    if isinstance(value, bool):
        raise SwapError("--warn must be a number from 1 to 100.")
    if not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise SwapError("--warn must be a number from 1 to 100.") from None
    if not math.isfinite(value) or not 1 <= value <= 100:
        raise SwapError("--warn must be a number from 1 to 100.")
    return float(value)


def parse_cache_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise SwapError(f"--cached SECONDS must be a positive number, got {value!r}.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise SwapError(f"--cached SECONDS must be a positive number, got {value!r}.")
    return seconds


def codex_windows(buckets: list[UsageBucket]) -> list[Any]:
    """The Codex quota bucket's windows, flattened; `quota_windows` with Codex's shape."""
    return quota_windows(buckets, CODEX_QUOTA)


def plain_codex_notice(manager: Manager, selected_home: str | Path) -> str | None:
    """Explain when the ordinary `codex` command still bypasses the xswap selection.

    Checks what a PATH lookup of `codex` runs, not only the entry recorded in
    auto.json (2026-09-10: a standalone install ahead of the wrapped entry bypassed
    xswap while the record looked fine), and names the classified reason when xswap
    deliberately left an entry alone -- including the `codex` a relative PATH element
    resolves to, which is what plain `codex` runs in this working directory and the one
    entry xswap can never wrap. Returns None when the entry plain `codex`
    runs is xswap-codex with automatic switching on, or when plain codex already
    uses the selected home. Only local labels and paths; never tokens.
    """
    from xswap.codex_cli import (
        BYPASS_REASON,
        RELATIVE_ENTRY_REASON,
        bypass_set,
        describe_drift,
        entry_drift,
        read_settings,
        reconnect_wrapper,
        wrapper_drift,
        wrapper_state,
    )
    try:
        reconnect_wrapper(manager)
        settings = read_settings(manager)
    except LiveError:
        settings = {}
    # relative=True: this notice only reports what plain `codex` runs, and the entry a relative
    # PATH element resolves to is exactly what it runs in this directory. reconnect_wrapper above
    # keeps the default, so nothing re-points it.
    state, first, _ = wrapper_state(settings, relative=True)
    # XSWAP_BYPASS=1 makes the entry point a pass-through, so a connected wrapper changes nothing
    # in this shell and the notice stayed silent about the one setting that decides it.
    bypassed = bypass_set() and state != "unconfigured"
    if state == "connected" and not bypassed:
        return None
    if Path(selected_home).expanduser().resolve() == manager.source:
        return None
    try:
        label = identity(manager.source)
    except SwapError:
        label = "unreadable auth cache"
    names = ",".join(name for name, _ in manager.enabled_accounts()) or "NAME,NAME"
    connect = f"xswap auto-enable --accounts {names} --wrap-codex"
    if bypassed:
        cause, fix = describe_drift({"action": "skip", "reason": BYPASS_REASON}, connect)
        return (f"Note: plain `codex` does not go through xswap here ({BYPASS_REASON}): {cause}, so it keeps using "
                f"{manager.source} ({label}); this selection applies only to xswap and xswap app. Fix: {fix}")
    if state in ("drifted", "shadowed", "relative"):
        assert first is not None  # these states are only reported with a first PATH entry  # noqa: S101 -- narrows an invariant the checker can't see across the call; not user input
        shown = f"{first['path']} -> {first['target']}" if first.get("target") else first["path"]
        # A relative entry is a skip by construction -- nothing may re-point one -- so it carries
        # entry_drift's shape with the reason whose cause and fix name the PATH element and the
        # working directory it resolves against, and the line below keeps the one shape every
        # other skip reason prints in.
        drift = ({"action": "skip", "reason": RELATIVE_ENTRY_REASON, "path": first["path"]} if state == "relative"
                 else wrapper_drift(settings) if state == "drifted"
                 else entry_drift(first["path"], (settings.get("wrapper") or {})["proxy"], adopt=True))
        if drift["action"] == "reconnect":
            fix_text = f"Connect it: {connect}"
        else:
            cause, fix = describe_drift(drift, connect)
            fix_text = f"xswap cannot wrap {first['path']} ({drift['reason']}): {cause}. Fix: {fix}"
        return (f"Note: plain `codex` runs {shown}, not xswap-codex, so it keeps using {manager.source} ({label}); "
                f"this selection applies only to xswap and xswap app. {fix_text}")
    drift = wrapper_drift(settings)
    cause, fix = describe_drift(drift, connect)
    if cause:
        return (f"Note: the codex command is not connected to xswap ({drift['reason']}): {cause}. "
                f"Plain `codex` keeps using {manager.source} ({label}); this selection applies only "
                f"to xswap and xswap app. Fix: {fix}")
    return ("Note: the codex command is not connected to xswap. Plain `codex` keeps using "
            f"{manager.source} ({label}); this selection applies only to xswap and xswap app. "
            f"Connect it: {connect}")


class _Hooks:
    """Late-bound lookups of this module's globals, for the collaborator modules.

    A collaborator that imported `read_limits` (or `identity`, `check_file_store`,
    `private_dir`, `parse_pool`, ...) directly would bind its own module's copy, and
    `patch("xswap.manager.read_limits")` -- which the suite uses 63 times -- would
    then patch a name nothing reads. Each method below resolves the name in *this*
    module's namespace at call time instead, so the patch still bites.

    Only the names the suite replaces today are routed here. Collaborators bind
    `rank_candidates`, `short_line`, `normalize_limits`, `is_sign_in_failure`,
    `describe_login_report` and `__version__` directly, so a future
    `patch("xswap.manager.rank_candidates")` would NOT reach them -- patch the
    collaborator's module, or add the name here first.
    """

    @staticmethod
    def identity(home: Path) -> str:
        return identity(home)

    @staticmethod
    def check_file_store(home: Path) -> None:
        return check_file_store(home)

    @staticmethod
    def private_dir(path: Path) -> None:
        return private_dir(path)

    @staticmethod
    def read_limits(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return read_limits(*args, **kwargs)

    @staticmethod
    def chatgpt_org_id(home: Path, name: str) -> str:
        return chatgpt_org_id(home, name)

    @staticmethod
    def parse_pool(value: str | list[str]) -> list[str]:
        return parse_pool(value)

    @staticmethod
    def resolve_openclaw_package_root(executable: str) -> Path | None:
        return resolve_openclaw_package_root(executable)


class Manager:
    """Facade over the collaborators; every method here delegates and adds nothing."""

    def __init__(self, root: str | Path | None = None, source: str | Path | None = None, create: bool = True) -> None:
        self.root = Path(root or os.environ.get(ROOT_VARIABLE, default_root())).expanduser().resolve()
        self.source = Path(source or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        # `create=False` for a caller that only reads state and must not invent it: the
        # xswap-codex entry point ran in whatever shell plain `codex` was typed in, so a shell
        # without CODEX_SWAP_HOME made it create an empty ~/.local/share/codex-swap on its way
        # to reporting that the real Codex was unavailable -- a second, stateless root that
        # `auto-disable` and `auto-enable --wrap-codex` then both refused to work with.
        if create or self.root.is_dir():
            private_dir(self.root)
        self.registry = self.root / "accounts.json"
        hooks = _Hooks()
        self._hooks = hooks
        # One provider per Manager today: Codex. `providers.get()` is the only way any
        # of this reaches a platform, and the account records name it (`"provider"`),
        # so a second platform is a new entry in `xswap.providers`, not a branch here.
        self.provider = providers.get(providers.DEFAULT)
        self._registry = Registry(self, hooks)
        self._mappings = Mappings(self, hooks)
        self._usage = UsageCache(self, hooks)
        self._auth = AuthState(self, hooks)
        self._openclaw = OpenClawSync(self, hooks)
        self._launcher = Launcher(self, hooks)

    # -- locking ---------------------------------------------------------------
    # The one lock of this root. Collaborators take it through these two methods and
    # never open a second descriptor on the same file: flock from the same process
    # would either self-deadlock or split a sequence that must stay atomic.

    def locked(self) -> Any:
        return locking.locked(self.root / ".lock")

    def try_locked(self) -> Any:
        """locked(), but yields False instead of waiting when someone else holds the lock.

        This lock is held across whole OpenClaw subprocesses (`sync-openclaw` waits up to
        90s for the SDK bridge and 45s for the Gateway reload), and it is taken on every
        launch, list, usage read and selection. A repair that only has to happen eventually
        must not sit behind that: `reconnect_wrapper` blocked every one of those commands
        for the length of a sync, and inside the alert job -- which runs each step under
        `timeout 120` -- the job was SIGTERMed before it ever reported. The work is
        idempotent and already silent on every skip, so the next launch does it instead.
        """
        return locking.try_locked(self.root / ".lock")

    # -- registry and selection ------------------------------------------------

    def read(self) -> RegistryData:
        return self._registry.read()

    def switch_target(self, target: str) -> str:
        return self._registry.switch_target(target)

    def account(self, name: str | None = None, mapped: bool = True) -> tuple[str, Path]:
        return self._registry.account(name, mapped)

    def register(self, name: str, home: str | Path | None = None) -> Path:
        return self._registry.register(name, home)

    def prepare(self, name: str) -> Path:
        return self._registry.prepare(name)

    def use(self, name: str) -> Path | bool:
        return self._use(name)

    def use_if_active(self, expected: str, name: str) -> Path | bool:
        """use(name), but only while `expected` is still the selected account; else False.

        `auto-tick` reads quota over the network (seconds), then switched if the selection
        still matched what it had decided about. That check and the write were two separate
        lock holds, so an `xswap use` -- or a second tick from the alert job, which the job's
        own overrun makes ordinary -- could land between them and be overwritten by a decision
        taken about the account it replaced. Compare and set under one lock instead; False is
        the caller's existing "a manual use landed, it wins" outcome.
        """
        return self._use(name, expected=expected)

    def _use(self, name: str, expected: Any = _UNSET) -> Path | bool:
        return self._registry.use(name, expected=expected)

    def select_if_unset(self, name: str) -> bool:
        return self._registry.select_if_unset(name)

    def require_enabled(self, name: str) -> None:
        return self._registry.require_enabled(name)

    def enabled_accounts(self) -> list[tuple[str, Path]]:
        return self._registry.enabled_accounts()

    def set_disabled(self, name: str, disabled: bool) -> None:
        return self._registry.set_disabled(name, disabled)

    def _auto_session_running(self, name: str) -> bool:
        return self._registry.auto_session_running(name)

    def purge_target(self, name: str, entry: AccountRecord) -> Path:
        return self._registry.purge_target(name, entry)

    def remove(self, name: str, purge: bool = False) -> dict[str, Any]:
        return self._registry.remove(name, purge)

    # -- directory mappings ----------------------------------------------------

    def _best_mapping(self, data: RegistryData, cwd: str | Path | None = None) -> tuple[int, str, str] | None:
        return self._mappings.best_mapping(data, cwd)

    def resolve_default(self, cwd: str | Path | None = None) -> tuple[str | None, str | None]:
        return self._mappings.resolve_default(cwd)

    def default_account(self, cwd: str | Path | None = None) -> str | None:
        return self._mappings.default_account(cwd)

    def mapped_source(self, cwd: str | Path | None = None) -> str | None:
        return self._mappings.mapped_source(cwd)

    def map_dir(self, name: str, path: str | Path | None = None) -> tuple[str, str]:
        return self._mappings.map_dir(name, path)

    def unmap_dir(self, path: str | Path | None = None) -> str:
        return self._mappings.unmap_dir(path)

    def list_mappings(self) -> dict[str, str]:
        return self._mappings.list_mappings()

    # -- usage cache and quota rows -------------------------------------------

    def usage_cache_path(self) -> Path:
        return self._usage.path()

    def cached_usage(self, name: str, label: str, max_age: float | None) -> dict[str, Any] | None:
        return self._usage.cached(name, label, max_age)

    def _forget_usage(self, name: str) -> None:
        """Caller holds the lock (see `remove` and `after_login`)."""
        return self._usage.forget(name)

    def _still_registered(self, name: str) -> bool:
        """Caller holds the lock."""
        return self._usage.still_registered(name)

    def remember_usage(
        self, name: str, buckets: list[UsageBucket], fetched_at: float, label: str, reset_credits: Any = None
    ) -> None:
        return self._usage.remember(name, buckets, fetched_at, label, reset_credits)

    def account_usage(
        self, name: str, home: Path, offline: bool = False, disabled: bool = False, max_age: float | None = None
    ) -> AccountUsageRow:
        return self._usage.account_usage(name, home, offline=offline, disabled=disabled, max_age=max_age)

    def account_rows(
        self, name: str | None = None, offline: bool = False, max_age: float | None = None
    ) -> list[AccountUsageRow]:
        return self._usage.account_rows(name, offline, max_age)

    def show_accounts(
        self,
        name: str | None = None,
        offline: bool = False,
        json_output: bool = False,
        include_spark: bool = False,
        details: bool = False,
        short: bool = False,
        max_age: float | None = None,
        lang: str = "en",
    ) -> list[AccountUsageRow]:
        return self._usage.show_accounts(name, offline, json_output, include_spark, details, short, max_age, lang)

    def best_account(
        self, model: str | None = None, exclude: tuple[str, ...] = (), max_age: float | None = None
    ) -> tuple[str | None, dict[str, Any]]:
        return self._usage.best_account(model, exclude, max_age)

    # -- auth-failure record ---------------------------------------------------

    def auth_state_path(self) -> Path:
        return self._auth.path()

    def _read_auth_state(self) -> dict[str, Any]:
        return self._auth.read()

    def auth_failure(self, name: str, label: str) -> dict[str, Any] | None:
        return self._auth.failure(name, label)

    def remember_auth_failure(
        self, name: str, label: str, reason: str = AUTH_FAILED_STATUS, failed_at: float | None = None
    ) -> None:
        return self._auth.remember(name, label, reason, failed_at)

    def _forget_auth_failure(self, name: str) -> None:
        """Caller holds the lock (see `remove` and `after_login`)."""
        return self._auth.forget(name)

    def clear_auth_failure(self, name: str) -> None:
        return self._auth.clear(name)

    # -- OpenClaw --------------------------------------------------------------

    def sync_openclaw(
        self,
        names: str | list[str] | None = None,
        agents: list[str] | None = None,
        dry: bool = False,
        backup_dir: str | Path | None = None,
        select: bool = False,
        allow_mixed: bool = False,
    ) -> dict[str, Any]:
        return self._openclaw.sync(names, agents, dry, backup_dir, select, allow_mixed)

    def clear_openclaw_cooldown(
        self,
        names: str | list[str] | None = None,
        dry: bool = False,
        yes: bool = False,
        backup_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        return self._openclaw.clear_cooldown(names, dry, yes, backup_dir)

    # -- launching -------------------------------------------------------------

    def env(self, home: Path) -> dict[str, str]:
        return self._launcher.env(home)

    def codex(self) -> str:
        return self._launcher.codex()

    def login(self, name: str | None, device_auth: bool = False, lang: str = "en") -> int:
        return self._launcher.login(name, device_auth, lang)

    def after_login(self, name: str, home: Path, lang: str = "en") -> None:
        return self._launcher.after_login(name, home, lang)

    def launch_cli(self, name: str | None, args: list[str], dry: bool = False) -> int:
        return self._launcher.launch_cli(name, args, dry)

    def launch_auto_app(self, accounts: str | None, app: str | None = None, dry: bool = False) -> int:
        return self._launcher.launch_auto_app(accounts, app, dry)

    def launch_app(self, name: str | None, app: str | None = None, dry: bool = False) -> int:
        return self._launcher.launch_app(name, app, dry)


# The command line moved to `xswap.cli` (INT-5610): `build_parser` assembles the
# subparsers and `main` dispatches to `xswap.cli.commands`. These two forwards stay
# because the `codex_swap` shim, `completion.py`, the help snapshots and the test
# suite all reach the CLI through this module. The import is inside the functions:
# `xswap.cli` imports this module at module level (it resolves `Manager` here so
# `patch("xswap.manager.Manager")` keeps biting), so importing it back at module
# level would be a cycle.

def parser() -> Any:
    """`xswap.cli.build_parser()`, under the name this module has always exported."""
    from xswap.cli import build_parser
    return build_parser()


def main(argv: list[str] | None = None) -> int:
    """`xswap.cli.main()`, under the name this module has always exported."""
    from xswap.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
