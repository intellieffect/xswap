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

import argparse
import json
import math
import os
from pathlib import Path
import shutil  # noqa: F401  patch target: xswap.manager.shutil.which
import subprocess  # noqa: F401  patch target: xswap.manager.subprocess.{run,call}
import sys
import time  # noqa: F401  patch target: xswap.manager.time

from xswap.usage import AUTH_FAILED_STATUS, UsageError, is_ok, is_sign_in_failure, normalize_limits, normalize_reset_credits, read_limits, short_line, window_label  # noqa: F401
from xswap.usage import warnings as usage_warnings
from xswap.display import resolve_lang
from xswap.live import LiveError, buckets_available, jwt_claims  # noqa: F401
from xswap.plugins import ensure_plugins  # noqa: F401
from xswap.relocate import link_packages  # noqa: F401
from xswap.credentials import CredentialError, read_auth  # noqa: F401
from xswap.upgrade import UpgradeError, upgrade
from xswap.alert import AlertError
from xswap.alert import install as alert_install, status as alert_status, uninstall as alert_uninstall
from xswap.path import absolute_which, relative_entry_message  # noqa: F401
from xswap import fsutil, locking
from xswap._version import __version__
from xswap.errors import SwapError
from xswap.fsutil import atomic_json  # noqa: F401  re-exported: tests and siblings import it from here
from xswap.paths import ROOT_VARIABLE, auto_dir, cli_runs_dir, default_root, profile_dir, settings_path  # noqa: F401

# Re-exported so `doctor`, `init`, `tick`, the `codex_swap` shim and the tests keep
# importing these from `xswap.manager`, which is where they have always lived.
from xswap.auth_state import AuthState
from xswap.identity import chatgpt_org_id, check_file_store, identity
from xswap.launcher import Launcher
from xswap.mappings import Mappings
from xswap.openclaw_sync import OpenClawSync, parse_pool, resolve_openclaw_package_root
from xswap.ranking import codex_windows, rank_candidates, window_percent  # noqa: F401
from xswap.registry import UNSET as _UNSET, Registry, validate_name  # noqa: F401
from xswap.reports import describe_login_report, describe_switch_report, failure_reasons_text  # noqa: F401
from xswap.usage_cache import UsageCache


def private_dir(path: Path):
    """fsutil.private_dir reporting through SwapError, this surface's error class.

    Kept as a function defined here (rather than a re-exported name) so it stays
    patchable on this module: `xswap.codex_cli` and `xswap.plugins` look it up
    through `xswap.manager` at call time.
    """
    return fsutil.private_dir(path, SwapError)


def validate_warn_threshold(value):
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


def parse_cache_seconds(value):
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise SwapError(f"--cached SECONDS must be a positive number, got {value!r}.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise SwapError(f"--cached SECONDS must be a positive number, got {value!r}.")
    return seconds


def is_auth_failed(manager, name):
    """True while the usage service's rejection of NAME's current login still stands.

    Module-level so callers and tests can reach it without a Manager method lookup;
    the state itself lives in Manager.auth_failure (auth-state.json).
    """
    try:
        _, home = manager.account(name)
        return manager.auth_failure(name, identity(home)) is not None
    except SwapError:
        return False


def plain_codex_notice(manager, selected_home):
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
    from xswap.codex_cli import (BYPASS_REASON, RELATIVE_ENTRY_REASON, bypass_set, describe_drift, entry_drift,
                           read_settings, reconnect_wrapper, wrapper_drift, wrapper_state)
    from xswap.live import LiveError
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
    """

    @staticmethod
    def identity(home):
        return identity(home)

    @staticmethod
    def check_file_store(home):
        return check_file_store(home)

    @staticmethod
    def private_dir(path):
        return private_dir(path)

    @staticmethod
    def read_limits(*args, **kwargs):
        return read_limits(*args, **kwargs)

    @staticmethod
    def chatgpt_org_id(home, name):
        return chatgpt_org_id(home, name)

    @staticmethod
    def parse_pool(value):
        return parse_pool(value)

    @staticmethod
    def resolve_openclaw_package_root(executable):
        return resolve_openclaw_package_root(executable)


class Manager:
    """Facade over the collaborators; every method here delegates and adds nothing."""

    def __init__(self, root=None, source=None, create=True):
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

    def locked(self):
        return locking.locked(self.root / ".lock")

    def try_locked(self):
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

    def read(self):
        return self._registry.read()

    def switch_target(self, target):
        return self._registry.switch_target(target)

    def account(self, name=None, mapped=True):
        return self._registry.account(name, mapped)

    def register(self, name, home=None):
        return self._registry.register(name, home)

    def prepare(self, name):
        return self._registry.prepare(name)

    def use(self, name):
        return self._use(name)

    def use_if_active(self, expected, name):
        """use(name), but only while `expected` is still the selected account; else False.

        `auto-tick` reads quota over the network (seconds), then switched if the selection
        still matched what it had decided about. That check and the write were two separate
        lock holds, so an `xswap use` -- or a second tick from the alert job, which the job's
        own overrun makes ordinary -- could land between them and be overwritten by a decision
        taken about the account it replaced. Compare and set under one lock instead; False is
        the caller's existing "a manual use landed, it wins" outcome.
        """
        return self._use(name, expected=expected)

    def _use(self, name, expected=_UNSET):
        return self._registry.use(name, expected=expected)

    def select_if_unset(self, name):
        return self._registry.select_if_unset(name)

    def require_enabled(self, name):
        return self._registry.require_enabled(name)

    def enabled_accounts(self):
        return self._registry.enabled_accounts()

    def set_disabled(self, name, disabled):
        return self._registry.set_disabled(name, disabled)

    def _auto_session_running(self, name):
        return self._registry.auto_session_running(name)

    def purge_target(self, name, entry):
        return self._registry.purge_target(name, entry)

    def remove(self, name, purge=False):
        return self._registry.remove(name, purge)

    # -- directory mappings ----------------------------------------------------

    def _best_mapping(self, data, cwd=None):
        return self._mappings.best_mapping(data, cwd)

    def resolve_default(self, cwd=None):
        return self._mappings.resolve_default(cwd)

    def default_account(self, cwd=None):
        return self._mappings.default_account(cwd)

    def mapped_source(self, cwd=None):
        return self._mappings.mapped_source(cwd)

    def map_dir(self, name, path=None):
        return self._mappings.map_dir(name, path)

    def unmap_dir(self, path=None):
        return self._mappings.unmap_dir(path)

    def list_mappings(self):
        return self._mappings.list_mappings()

    # -- usage cache and quota rows -------------------------------------------

    def usage_cache_path(self):
        return self._usage.path()

    def cached_usage(self, name, label, max_age):
        return self._usage.cached(name, label, max_age)

    def _forget_usage(self, name):
        """Caller holds the lock (see `remove` and `after_login`)."""
        return self._usage.forget(name)

    def _still_registered(self, name):
        """Caller holds the lock."""
        return self._usage.still_registered(name)

    def remember_usage(self, name, buckets, fetched_at, label, reset_credits=None):
        return self._usage.remember(name, buckets, fetched_at, label, reset_credits)

    def account_usage(self, name, home, offline=False, disabled=False, max_age=None):
        return self._usage.account_usage(name, home, offline=offline, disabled=disabled, max_age=max_age)

    def account_rows(self, name=None, offline=False, max_age=None):
        return self._usage.account_rows(name, offline, max_age)

    def show_accounts(self, name=None, offline=False, json_output=False, include_spark=False, details=False, short=False, max_age=None, lang="en"):
        return self._usage.show_accounts(name, offline, json_output, include_spark, details, short, max_age, lang)

    def best_account(self, model=None, exclude=(), max_age=None):
        return self._usage.best_account(model, exclude, max_age)

    # -- auth-failure record ---------------------------------------------------

    def auth_state_path(self):
        return self._auth.path()

    def _read_auth_state(self):
        return self._auth.read()

    def auth_failure(self, name, label):
        return self._auth.failure(name, label)

    def remember_auth_failure(self, name, label, reason=AUTH_FAILED_STATUS, failed_at=None):
        return self._auth.remember(name, label, reason, failed_at)

    def _forget_auth_failure(self, name):
        """Caller holds the lock (see `remove` and `after_login`)."""
        return self._auth.forget(name)

    def clear_auth_failure(self, name):
        return self._auth.clear(name)

    # -- OpenClaw --------------------------------------------------------------

    def sync_openclaw(self, names=None, agents=None, dry=False, backup_dir=None, select=False, allow_mixed=False):
        return self._openclaw.sync(names, agents, dry, backup_dir, select, allow_mixed)

    def clear_openclaw_cooldown(self, names=None, dry=False, yes=False, backup_dir=None):
        return self._openclaw.clear_cooldown(names, dry, yes, backup_dir)

    # -- launching -------------------------------------------------------------

    def env(self, home):
        return self._launcher.env(home)

    def codex(self):
        return self._launcher.codex()

    def login(self, name, device_auth=False, lang="en"):
        return self._launcher.login(name, device_auth, lang)

    def after_login(self, name, home, lang="en"):
        return self._launcher.after_login(name, home, lang)

    def launch_cli(self, name, args, dry=False):
        return self._launcher.launch_cli(name, args, dry)

    def launch_auto_app(self, accounts, app=None, dry=False):
        return self._launcher.launch_auto_app(accounts, app, dry)

    def launch_app(self, name, app=None, dry=False):
        return self._launcher.launch_app(name, app, dry)

def parser():
    p = argparse.ArgumentParser(description="Codex account switcher for CLI + macOS desktop. Bare xswap opens the selected CLI account.")
    p.add_argument("--version", action="version", version=f"xswap {__version__}")
    sub = p.add_subparsers(dest="command")
    ini = sub.add_parser("init", help="First-run setup: register, add accounts, enable auto mode, set policy, then doctor")
    ini.add_argument("--yes", action="store_true", help="Non-interactive: accept every safe default, prompt for nothing")
    ini.add_argument("--no-auto", action="store_true", dest="no_auto", help="Skip the automatic-switching step")
    ini.add_argument("--weekly-remaining", type=float, help="Weekly remaining percentage for auto-policy (default 10)")
    r = sub.add_parser("register", help="Register an existing signed-in Codex home without copying its tokens")
    r.add_argument("name"); r.add_argument("--home", type=Path)
    a = sub.add_parser("add", help="Sign in to an isolated account home")
    a.add_argument("name"); a.add_argument("--device-auth", action="store_true")
    a.add_argument("--prepare-only", action="store_true")
    a.add_argument("--use", action="store_true", help="Select this account immediately after signing in")
    lg = sub.add_parser("login", help="Re-authenticate a registered account whose login expired")
    lg.add_argument("name"); lg.add_argument("--device-auth", action="store_true")
    listing = sub.add_parser("list", help="List accounts with live remaining quotas and reset times")
    listing.add_argument("--offline", action="store_true", help="Show local account labels without fetching usage")
    listing.add_argument("--json", action="store_true", dest="json_output")
    listing.add_argument("--include-spark", action="store_true", help="Include Spark quotas in text output")
    listing.add_argument("--details", action="store_true", help="Show identity and all quota window details")
    listing.add_argument("--short", action="store_true", help="Print one line for a status line/prompt: `*name p5h/p7d · ...`")
    listing.add_argument("--warn", metavar="PCT",
                          help="Print a warning per codex window below PCT remaining (1-100) to stderr and exit 3")
    listing.add_argument("--lang", choices=["en", "ko"], help="Text output language")
    usage = sub.add_parser("usage", help="Show live quota windows for the selected or named account")
    usage.add_argument("name", nargs="?")
    usage.add_argument("--json", action="store_true", dest="json_output")
    usage.add_argument("--include-spark", action="store_true", help="Include Spark quotas in text output")
    usage.add_argument("--details", action="store_true", help="Show identity and all quota window details")
    usage.add_argument("--short", action="store_true", help="Print one line: `name p5h/p7d`")
    usage.add_argument("--lang", choices=["en", "ko"], help="Text output language")
    listing.add_argument("--cached", metavar="SECONDS", help="Reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex (not with --offline)")
    usage.add_argument("--cached", metavar="SECONDS", help="Reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    sub.add_parser("menubar", help="Build and open the macOS weekly quota menu")
    dash = sub.add_parser("dashboard", help="Private presentation JSON for the menu app")
    dash.add_argument("--lang", choices=["en", "ko"], help="Text output language")
    sub.add_parser("status", help="Show selected account and login status")
    st = sub.add_parser("auto-status", help="Show desktop/CLI automatic switching state (no credentials)")
    st.add_argument("--prune", action="store_true", help="Remove every non-running CLI run record now, not only stopped ones older than a day, empty ones older than a minute, and others older than 7 days")
    tick = sub.add_parser("auto-tick", help="One proactive check for launchd/cron: if the selected account is at or below the weekly reserve, select the best pool account (exit 0 switched, 1 error, 2 no action, 3 blocked)")
    tick.add_argument("--dry-run", action="store_true", help="Print the decision without selecting or signalling anything")
    tick.add_argument("--cached", metavar="SECONDS", help="Reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    tick.add_argument("--json", action="store_true", dest="json_output", help="Machine-readable decision (names, percentages, short reasons; no tokens)")
    ae = sub.add_parser("auto-enable", help="Enable automatic switching for new xswap CLI/app sessions")
    ae.add_argument("--accounts", required=True)
    ae.add_argument("--wrap-codex", action="store_true", help="Also wrap the user-owned codex symlink, with rollback metadata")
    policy = sub.add_parser("auto-policy", help="Set weekly remaining percentage for proactive switching")
    policy.add_argument("--weekly-remaining", type=float, required=True)
    rp = sub.add_parser("repair-plugins", help="Materialize legacy shared plugin links without stopping sessions")
    rp.add_argument("--dry-run", action="store_true")
    rc = sub.add_parser("relocate-codex", help="Move a Codex release that an in-session update installed inside xswap's state directory to the reference Codex home, and link xswap's homes to it")
    rc.add_argument("--dry-run", action="store_true", help="Print the planned moves without changing anything")
    dr = sub.add_parser("doctor", help="Diagnose codex install, accounts, plugins, and OpenClaw (read-only, no network)")
    dr.add_argument("--json", action="store_true", dest="json_output")
    sub.add_parser("auto-disable", help="Disable auto defaults and restore the codex symlink")
    up = sub.add_parser("upgrade", help="Reinstall xswap from the latest (or a chosen) released Git tag")
    up.add_argument("--tag", help="Install this tag instead of the latest release, e.g. v0.5.1")
    up.add_argument("--dry-run", action="store_true")
    al = sub.add_parser("alert", help="Install, remove, or inspect a launchd job that polls `list --warn` and posts macOS notifications")
    al.add_argument("--install", action="store_true", help="Write the wrapper script and plist, then load it")
    al.add_argument("--uninstall", action="store_true", help="Unload the job and delete the wrapper script and plist")
    al.add_argument("--status", action="store_true", help="Show whether the job is installed and loaded, and the last log tail")
    al.add_argument("--dry-run", action="store_true", help="Print what --install/--uninstall would do without changing anything")
    al.add_argument("--warn", type=float, default=15, metavar="PCT", help="Threshold passed to `list --warn` (1-100, default 15)")
    al.add_argument("--every", type=int, default=30, metavar="MINUTES", help="Polling interval in whole minutes (default 30; launchd merges under 60s)")
    al.add_argument("--cached", type=float, default=600, metavar="SECONDS", help="Freshness passed to `list --cached` (default 600)")
    al.add_argument("--auto-switch", action="store_true", help="With --install: run `xswap auto-tick --cached SECONDS` before the warn step and post a notification when it switches")
    u = sub.add_parser("use", aliases=["switch"], help="Select the default account for xswap and xswap app")
    u.add_argument("name", nargs="?", help="Account name; switch also accepts a 1-based slot from xswap list")
    u.add_argument("--best", action="store_true", help="Select the account with the most remaining quota right now")
    u.add_argument("--model", help="Model hint for --best; never passed to codex")
    u.add_argument("--cached", metavar="SECONDS", help="With --best, reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    u.add_argument("--default-only", action="store_true", help="Change only the default for future sessions")
    u.add_argument("--openclaw", action="store_true", help="Also update all local OpenClaw agents and reload Gateway auth")
    d = sub.add_parser("disable", help="Hold an account out of selection without deleting it")
    d.add_argument("name")
    en = sub.add_parser("enable", help="Restore a disabled account to selection")
    en.add_argument("name")
    rm = sub.add_parser("remove", help="Drop an account's registry entry; files are kept unless --purge")
    rm.add_argument("name")
    rm.add_argument("--purge", action="store_true", help="Also delete the managed profile directory (a registered home is never deleted)")
    rm.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    mp = sub.add_parser("map", help="Map a directory to a default account, or list existing mappings")
    mp.add_argument("name", nargs="?")
    mp.add_argument("path", nargs="?", type=Path)
    um = sub.add_parser("unmap", help="Remove a directory's account mapping")
    um.add_argument("path", nargs="?", type=Path)
    o = sub.add_parser("openclaw", help="Sync an account (or pool) to local OpenClaw agents and reload Gateway auth")
    o.add_argument("name", nargs="?")
    o.add_argument("--pool", help="Comma-separated accounts (>=2) OpenClaw rotates between on its own cooldowns; mutually exclusive with NAME")
    o.add_argument("--allow-mixed", action="store_true", help="Allow --pool accounts from different ChatGPT organizations")
    o.add_argument("--agent", action="append", dest="agents", help="Target only this agent (repeatable; default: all)")
    o.add_argument("--dry-run", action="store_true")
    o.add_argument("--backup-dir", type=Path, help="Directory for small, private auth-state backups")
    o.add_argument("--clear-cooldown", action="store_true",
                    help="Clear a stale OpenClaw auth-profile cooldown for NAME/--pool (or every registered account), restarting the local Gateway")
    o.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    r = sub.add_parser("run", help="Run Codex with the selected or named account")
    r.add_argument("--account"); r.add_argument("--dry-run", action="store_true")
    r.add_argument("--auto", action="store_true", help="Keep the interactive CLI session alive across quota account switches")
    r.add_argument("--accounts", help="Explicit automatic fallback order")
    r.add_argument("--best", action="store_true", help="Launch with the account with the most remaining quota right now (one-shot; not live switching)")
    r.add_argument("--model", help="Model hint for --best; never passed to codex")
    r.add_argument("--cached", metavar="SECONDS", help="With --best, reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    r.add_argument("args", nargs=argparse.REMAINDER)
    a = sub.add_parser("app", help="Open an account-specific desktop instance")
    a.add_argument("name", nargs="?"); a.add_argument("--app"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("--auto", action="store_true", help="Keep one desktop session and switch accounts after quota exhaustion (experimental)")
    a.add_argument("--accounts", help="Explicit fallback order, e.g. work,main; requires --auto")
    comp = sub.add_parser("completion", help="Print a shell completion script (see xswap_completion.py)")
    comp.add_argument("shell", choices=["zsh", "bash"])
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        manager = Manager()
        if args.command == "init":
            from xswap.init import run_init
            return run_init(manager, args)
        elif args.command == "register":
            home = manager.register(args.name, args.home)
            print(f"Registered {args.name}: {identity(home)}. Credentials stay at {home}.")
        elif args.command == "add":
            home = manager.prepare(args.name)
            if args.prepare_only:
                print(f"Prepared {args.name}. Sign in: xswap add {args.name}")
                return 0
            if identity(home) not in ("not signed in", "unreadable auth cache"):
                raise SwapError(f"{args.name} already has a login. Re-authenticate with: xswap login {args.name}")
            command = [manager.codex(), "login"] + (["--device-auth"] if args.device_auth else [])
            result = subprocess.call(command, env=manager.env(home))
            if result:
                return result
            label = identity(home)
            if label in ("not signed in", "unreadable auth cache"):
                raise SwapError(f"codex login exited successfully, but {args.name} has no readable login at {home}. The sign-in may have been cancelled; run xswap add {args.name} again.")
            print(f"Saved {args.name}: {label}. Select it: xswap use {args.name}")
            manager.after_login(args.name, home, lang=resolve_lang(getattr(args, "lang", None), os.environ))
            # The account must be signed in before it can become active, so this only runs after login succeeds.
            if args.use:
                manager.use(args.name)
                print(f"Selected {args.name}.")
            elif manager.select_if_unset(args.name):
                print(f"Selected {args.name} (first account).")
        elif args.command == "login":
            return manager.login(args.name, args.device_auth, lang=resolve_lang(getattr(args, "lang", None), os.environ))
        elif args.command == "list":
            threshold = validate_warn_threshold(args.warn) if args.warn is not None else None
            max_age = parse_cache_seconds(args.cached)
            if args.offline and max_age is not None:
                raise SwapError("--offline and --cached cannot be combined.")
            lang = resolve_lang(args.lang or "en", os.environ)
            rows = manager.show_accounts(offline=args.offline, json_output=args.json_output, include_spark=args.include_spark, details=args.details, short=args.short, max_age=max_age, lang=lang)
            if threshold is not None:
                messages = usage_warnings(rows, threshold)
                for message in messages:
                    print(message, file=sys.stderr)
                return 3 if messages else 0
        elif args.command == "usage":
            max_age = parse_cache_seconds(args.cached)
            name, _ = manager.account(args.name)
            lang = resolve_lang(args.lang, os.environ)
            manager.show_accounts(name=name, json_output=args.json_output, include_spark=args.include_spark, details=args.details, short=args.short, max_age=max_age, lang=lang)
        elif args.command == "dashboard":
            from xswap.display import dashboard
            lang = resolve_lang(args.lang, os.environ)
            print(json.dumps(dashboard(manager, lang=lang), ensure_ascii=False))
        elif args.command == "menubar":
            from xswap.menubar import launch
            return launch()
        elif args.command == "status":
            default_name, mapped = manager.resolve_default()
            name, home = manager.account(default_name)
            lines = [f"Selected: {name}", f"Home: {home}", f"Local label: {identity(home)}"]
            if mapped:
                lines.append(f"Mapped by: {mapped}")
            print("\n".join(lines), flush=True)
            return manager.launch_cli(name, ["login", "status"])
        elif args.command == "auto-policy":
            from xswap.codex_cli import set_policy
            set_policy(manager, args.weekly_remaining)
        elif args.command == "repair-plugins":
            from xswap.plugins import repair
            repair(manager, args.dry_run)
        elif args.command == "relocate-codex":
            from xswap.relocate import relocate
            relocate(manager, dry=args.dry_run)
        elif args.command == "doctor":
            from xswap.doctor import run, print_report
            return print_report(run(manager), args.json_output)
        elif args.command == "auto-enable":
            from xswap.codex_cli import enable
            enable(manager, args.accounts, args.wrap_codex)
        elif args.command == "auto-disable":
            from xswap.codex_cli import disable
            disable(manager)
        elif args.command == "auto-status":
            from xswap.codex_cli import show_status
            show_status(manager, args.prune)
        elif args.command == "auto-tick":
            from xswap.tick import run_tick
            return run_tick(manager, dry_run=args.dry_run, max_age=parse_cache_seconds(args.cached), json_output=args.json_output)
        elif args.command == "upgrade":
            return upgrade(__version__, args.tag, args.dry_run)
        elif args.command == "alert":
            if sum((args.install, args.uninstall, args.status)) != 1:
                raise SwapError("Give exactly one of --install, --uninstall, or --status.")
            if args.auto_switch and not args.install:
                raise SwapError("--auto-switch requires --install.")
            if args.install:
                return alert_install(manager.root, warn=args.warn, every=args.every, cached=args.cached,
                                     auto_switch=args.auto_switch, dry_run=args.dry_run)
            if args.uninstall:
                return alert_uninstall(manager.root, dry_run=args.dry_run)
            return alert_status(manager.root)
        elif args.command in ("use", "switch"):
            if bool(args.name) == bool(args.best):
                raise SwapError("Give an account name or --best.")
            if args.model and not args.best:
                raise SwapError("--model requires --best.")
            if args.cached is not None and not args.best:
                raise SwapError("--cached requires --best.")
            detail = ""
            if args.best:
                max_age = parse_cache_seconds(args.cached)
                name, reason = manager.best_account(args.model, max_age=max_age)
                if name is None:
                    raise SwapError("No account with known remaining quota; nothing selected.")
                parts = [f"{label} {value:g}% left" for label, value in reason["remaining"].items() if value is not None]
                if parts:
                    detail = f" ({', '.join(parts)})"
            else:
                name = manager.switch_target(args.name) if args.command == "switch" else args.name
            if args.openclaw:
                manager.sync_openclaw(name, select=True)
            else:
                manager.use(name)
            print(f"Selected {name}{detail}. CLI: xswap · Desktop: xswap app")
            notice = plain_codex_notice(manager, manager.account(name)[1])
            if notice:
                print(notice)
            if not args.default_only:
                from xswap.switch import switch_running
                report = switch_running(manager, name)
                print(describe_switch_report(report, name))
                if report.get('failed') or report.get('unconfirmed') or report.get('unsafe'):
                    return 1
        elif args.command == "disable":
            manager.set_disabled(args.name, True)
            print(f"Disabled {args.name}. It is skipped by use, --auto pools, and live usage fetches. Restore it: xswap enable {args.name}")
        elif args.command == "enable":
            manager.set_disabled(args.name, False)
            print(f"Enabled {args.name}. Select it: xswap use {args.name}")
        elif args.command == "remove":
            name, _ = manager.account(args.name)
            entry = manager.read()["accounts"][name]
            will_purge = args.purge and entry.get("managed")
            # The same directory remove() will delete, refused here when the record names
            # another one -- before the prompt offers to delete it.
            target = manager.purge_target(name, entry) if will_purge else None
            if args.purge and not entry.get("managed"):
                print(f"--purge is ignored for {name}: it is a registered home, not a managed profile.")
            if not args.yes:
                if not sys.stdin.isatty():
                    raise SwapError("Confirm with --yes.")
                prompt = (f"Remove {name} from xswap and delete {target}? [y/N] " if will_purge
                          else f"Remove {name} from xswap? [y/N] ")
                if input(prompt).strip().lower() != "y":
                    print("Aborted.")
                    return 1
            result = manager.remove(name, purge=args.purge)
            if result["purged"]:
                print(f"Removed {name}. Deleted the managed profile at {target}.")
            else:
                print(f"Removed {name} from xswap. Files kept at {result['kept']}.")
        elif args.command == "map":
            if args.name is None:
                mappings = manager.list_mappings()
                if not mappings:
                    print("No directory mappings.")
                else:
                    for path, mapped_name in sorted(mappings.items()):
                        print(f"{path} → {mapped_name}")
            else:
                path, name = manager.map_dir(args.name, args.path)
                print(f"Mapped {path} to {name}.")
        elif args.command == "unmap":
            path = manager.unmap_dir(args.path)
            print(f"Unmapped {path}.")
        elif args.command == "app":
            if args.auto:
                if args.name:
                    raise SwapError("Use --accounts instead of a positional name with --auto.")
                return manager.launch_auto_app(args.accounts, args.app, args.dry_run)
            if args.accounts:
                raise SwapError("--accounts requires --auto.")
            from xswap.codex_cli import read_settings
            settings = read_settings(manager)
            if args.name is None and settings.get("enabled"):
                return manager.launch_auto_app(','.join(settings["accounts"]), args.app, args.dry_run)
            return manager.launch_app(args.name, args.app, args.dry_run)
        elif args.command == "openclaw":
            if args.pool and args.name:
                raise SwapError("--pool and a positional NAME are mutually exclusive.")
            if args.allow_mixed and not args.pool:
                raise SwapError("--allow-mixed requires --pool.")
            # Only split here; sync_openclaw's own parse_pool() call is the single place
            # that dedupes and enforces >=2 distinct names, so this list isn't re-validated.
            names = args.pool.split(",") if args.pool else args.name
            if args.clear_cooldown:
                if args.allow_mixed:
                    raise SwapError("--allow-mixed has no effect with --clear-cooldown.")
                if args.agents:
                    raise SwapError("--agent has no effect with --clear-cooldown.")
                manager.clear_openclaw_cooldown(names, dry=args.dry_run, yes=args.yes, backup_dir=args.backup_dir)
            else:
                if args.yes:
                    raise SwapError("--yes requires --clear-cooldown.")
                manager.sync_openclaw(names, args.agents, args.dry_run, args.backup_dir, allow_mixed=args.allow_mixed)
        elif args.command == "completion":
            from xswap.completion import generate
            sys.stdout.write(generate(parser(), args.shell))
        else:
            rest = getattr(args, "args", [])
            if rest[:1] == ["--"]:
                rest = rest[1:]
            if getattr(args, "best", False):
                if args.account or getattr(args, "auto", False):
                    raise SwapError("--best cannot be combined with --account or --auto.")
                if getattr(args, "accounts", None):
                    raise SwapError("--accounts requires --auto.")
                max_age = parse_cache_seconds(getattr(args, "cached", None))
                name, reason = manager.best_account(getattr(args, "model", None), max_age=max_age)
                if name is None:
                    name, _ = manager.account()
                    print(f"xswap: no account with known remaining quota; using {name}", file=sys.stderr)
                if args.dry_run:
                    _, home = manager.account(name)
                    print(json.dumps({"account": name, "reason": reason, "CODEX_HOME": str(home),
                                      "argv": [manager.codex(), *rest]}, indent=2))
                    return 0
                return manager.launch_cli(name, rest, False)
            if getattr(args, "cached", None) is not None:
                raise SwapError("--cached requires --best.")
            if getattr(args, "auto", False):
                if args.account or not args.accounts:
                    raise SwapError("Use --auto --accounts first,second without --account.")
                from xswap.codex_cli import launch_cli
                return launch_cli(manager, args.accounts, rest, args.dry_run)
            if getattr(args, "accounts", None):
                raise SwapError("--accounts requires --auto.")
            return manager.launch_cli(getattr(args, "account", None), rest, getattr(args, "dry_run", False))
        return 0
    except (SwapError, LiveError, UpgradeError, AlertError, OSError) as exc:
        print(f"xswap: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except subprocess.TimeoutExpired:
        print("xswap: Operation timed out; OpenClaw may be partially updated. Check xswap's auth backups and retry the same command. No existing sessions were stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
