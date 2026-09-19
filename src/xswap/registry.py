"""The account registry: accounts.json and everything that reads or rewrites it.

Holds no lock of its own. Every read-modify-write here runs inside the single
`Manager.locked()` acquisition the facade takes, exactly as it did when these
bodies lived on `Manager`, and the cross-concern steps (`remove` forgetting the
usage-cache and auth-state entries) call the facade's lock-free `_forget_*`
helpers so one sequence keeps one lock.

Anything patchable -- `Manager` methods, and the module globals of
`xswap.manager` -- is reached back through `self.manager` / `self.hooks` rather
than imported here, so existing patches still bite.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shutil

from xswap.errors import SwapError
from xswap.fsutil import atomic_json
from xswap.paths import auto_dir, cli_runs_dir, profile_dir, settings_path

# "no expected selection was pinned" for Registry.use. A sentinel rather than None, because
# None is a real selection state (no account selected) a caller may legitimately pin against.
UNSET = object()


def validate_name(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", name):
        raise SwapError("Name must be 1–40 letters, digits, underscores or hyphens.")
    return name


class Registry:
    def __init__(self, manager, hooks):
        self.manager = manager
        self.hooks = hooks

    @property
    def path(self):
        return self.manager.registry

    def read(self):
        if not self.path.exists():
            return {"version": 1, "active": None, "accounts": {}}
        try:
            data = json.loads(self.path.read_text())
            if (data.get("version") != 1 or not isinstance(data.get("accounts"), dict) or
                    ("mappings" in data and not isinstance(data["mappings"], dict))):
                raise ValueError()
            return data
        except (ValueError, OSError):
            raise SwapError("Invalid account registry; refusing to overwrite it.") from None

    def switch_target(self, target):
        """Resolve a 1-based list position; use NAME remains literal for numeric names."""
        if re.fullmatch(r"[0-9]+", target):
            names = list(self.manager.read()["accounts"])
            slot = int(target) if len(target) <= 40 else 0
            if not 1 <= slot <= len(names):
                raise SwapError("Invalid account slot. Run: xswap list")
            return names[slot - 1]
        return target

    def account(self, name=None, mapped=True):
        data = self.manager.read()
        name = name or (self.manager.default_account() if mapped else data["active"])
        if name not in data["accounts"]:
            # ASCII-only hint text: some terminals/log pipelines mangle non-ASCII dashes.
            hint = " -- no accounts yet -- run xswap init" if not data["accounts"] else ""
            raise SwapError(f"No matching account. Run: xswap register main, or xswap add NAME{hint}")
        return name, Path(data["accounts"][name]["home"])

    def register(self, name, home=None):
        validate_name(name)
        home = Path(home or self.manager.source).expanduser().resolve()
        self.hooks.check_file_store(home)
        if self.hooks.identity(home) in ("not signed in", "unreadable auth cache"):
            raise SwapError(f"No readable login at {home}. Use xswap add {name} to sign in.")
        with self.manager.locked():
            data = self.manager.read()
            if name in data["accounts"]:
                if data["accounts"][name]["home"] == str(home):
                    return home
                raise SwapError(f"Account {name!r} already exists.")
            if any(a["home"] == str(home) for a in data["accounts"].values()):
                raise SwapError("This Codex home is already registered under another name.")
            data["accounts"][name] = {"home": str(home), "managed": False}
            data["active"] = data["active"] or name
            atomic_json(self.path, data)
        return home

    def prepare(self, name):
        from xswap.plugins import ensure_plugins
        from xswap.relocate import link_packages
        validate_name(name)
        self.hooks.check_file_store(self.manager.source)
        with self.manager.locked():
            data = self.manager.read()
            if name in data["accounts"]:
                return Path(data["accounts"][name]["home"])
            home = profile_dir(self.manager.root, name) / "codex"
            self.hooks.private_dir(home.parent)
            self.hooks.private_dir(home)
            # Share tooling, but never credentials, sessions, databases or app cookies.
            for entry in ("config.toml", "AGENTS.md", "skills", "rules"):
                src, dst = self.manager.source / entry, home / entry
                if src.exists() and not dst.exists() and not dst.is_symlink():
                    dst.symlink_to(src, target_is_directory=src.is_dir())
            ensure_plugins(home, self.manager.source)
            link_packages(self.manager, home, data["accounts"])
            data["accounts"][name] = {"home": str(home), "managed": True}
            atomic_json(self.path, data)
        return home

    def use(self, name, expected=UNSET):
        with self.manager.locked():
            # First thing inside the lock: a caller pinning the selection must not raise on the
            # new account's state (disabled, signed out) when the answer is "someone else chose".
            if expected is not UNSET and self.manager.read()["active"] != expected:
                return False
            name, home = self.manager.account(name)
            self.manager.require_enabled(name)
            self.hooks.check_file_store(home)
            label = self.hooks.identity(home)
            if label in ("not signed in", "unreadable auth cache"):
                raise SwapError(f"Account {name} is not signed in. Run: xswap add {name}")
            if self.manager.auth_failure(name, label) is not None:
                raise SwapError(f"Account {name} needs a new login: the usage service rejected it. "
                                f"Run: xswap login {name} (a live xswap usage {name} re-checks it)")
            data = self.manager.read()
            # New automatic CLI/app sessions must follow the same selection as
            # the live-switch broadcast, even when it was outside the old pool.
            from xswap.codex_cli import read_settings
            settings = read_settings(self.manager)
            if settings.get('enabled'):
                settings['accounts'] = list(dict.fromkeys([name, *settings.get('accounts', [])]))
                atomic_json(settings_path(self.manager.root), settings)
            data["active"] = name
            atomic_json(self.path, data)
        return home

    def select_if_unset(self, name):
        # Mirrors use(): the None check, the signed-in check, and the write share one lock
        # so a concurrent add/use cannot race between "no active account" and setting one.
        with self.manager.locked():
            name, home = self.manager.account(name)
            self.manager.require_enabled(name)
            self.hooks.check_file_store(home)
            if self.hooks.identity(home) in ("not signed in", "unreadable auth cache"):
                raise SwapError(f"Account {name} is not signed in. Run: xswap add {name}")
            data = self.manager.read()
            if data["active"] is not None:
                return False
            data["active"] = name
            atomic_json(self.path, data)
        return True

    def require_enabled(self, name):
        data = self.manager.read()
        if data["accounts"][name].get("disabled"):
            raise SwapError(f"Account {name} is disabled. Run: xswap enable {name}")

    def enabled_accounts(self):
        data = self.manager.read()
        return [(name, Path(value["home"])) for name, value in data["accounts"].items() if not value.get("disabled")]

    def set_disabled(self, name, disabled):
        with self.manager.locked():
            name, _ = self.manager.account(name)
            data = self.manager.read()
            if disabled:
                data["accounts"][name]["disabled"] = True
            else:
                data["accounts"][name].pop("disabled", None)
            atomic_json(self.path, data)

    def auto_session_running(self, name):
        """Best effort: report whether a live auto bridge (desktop or CLI) holds this account.

        Both the account the bridge runs on right now (`account`) and the pool it may
        switch to (`accounts`, recorded since 0.8.0) count. Reading only `account` let
        `remove --purge` delete the login a running bridge would have moved to when its
        current account hits the weekly limit: the bridge keeps running, and the switch
        it was set up to make then fails with "selected account is disabled or removed"
        against a profile directory that no longer exists. The pool is a claim on the
        account for as long as the session lives, exactly like the current selection.
        """
        candidates = [auto_dir(self.manager.root) / ".bridge.lock"]
        cli_runs = cli_runs_dir(self.manager.root)
        try:
            if cli_runs.is_dir():
                candidates += [run_dir / ".bridge.lock" for run_dir in cli_runs.iterdir() if run_dir.is_dir()]
        except OSError:
            return False
        for lock_path in candidates:
            try:
                if not lock_path.exists():
                    continue
                fd = os.open(lock_path, os.O_RDONLY)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        state = json.loads((lock_path.parent / "status.json").read_text())
                    except (OSError, ValueError):
                        state = None
                    if isinstance(state, dict):
                        pool = state.get("accounts")
                        if state.get("account") == name or (isinstance(pool, list) and name in pool):
                            return True
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return False

    def purge_target(self, name, entry):
        """The directory `--purge` deletes for a managed account: the profile its record names.

        `remove --purge` deleted `<root>/profiles/<name>` and never compared it with the
        recorded `home`. The two agree only for a profile this root created and nothing has
        moved since; when they differ -- a registry restored or copied under another
        CODEX_SWAP_HOME, a hand-edited `home` -- the purge reported "Deleted the managed
        profile at ..." while the login it had just dropped stayed on disk, and it deleted
        whatever the convention path happened to hold instead. Only a human can say which of
        the two was meant, so name both and delete neither.
        """
        target = profile_dir(self.manager.root, name)
        recorded = Path(entry["home"])
        if recorded != target / "codex":
            raise SwapError(f"{name}'s recorded home is {recorded}, but --purge deletes the managed profile at "
                            f"{target}, which is not where that login lives. Refusing to delete a directory this "
                            f"registry does not name: run xswap remove {name} without --purge, then delete "
                            f"{recorded} yourself if you no longer want it.")
        return target

    def remove(self, name, purge=False):
        """Registry drop plus the usage-cache and auth-state entries, under ONE lock hold.

        The two `_forget_*` calls are the facade's lock-free helpers precisely so this
        whole sequence stays a single acquisition of a single lock file.
        """
        from xswap.codex_cli import read_settings
        with self.manager.locked():
            name, home = self.manager.account(name)
            settings = read_settings(self.manager)
            if settings.get("enabled") and name in settings.get("accounts", []):
                raise SwapError(f"{name} is in the automatic switching pool. Run xswap auto-disable or auto-enable with a new pool first.")
            if self.manager._auto_session_running(name):
                raise SwapError(f"{name} is used by a running auto session.")
            data = self.manager.read()
            # Before anything is dropped: a purge that cannot name its directory must leave the
            # registry entry in place, so the account can still be removed without --purge.
            target = self.manager.purge_target(name, data["accounts"][name]) if purge and data["accounts"][name].get("managed") else None
            entry = data["accounts"].pop(name)
            if data["active"] == name:
                data["active"] = None
            atomic_json(self.path, data)
            self.manager._forget_usage(name)
            self.manager._forget_auth_failure(name)
            purged = False
            kept = entry["home"]
            if target is not None:
                resolved = target.resolve()
                if (resolved != target or resolved.is_symlink() or not resolved.is_dir()
                        or resolved.stat().st_uid != os.getuid()):
                    raise SwapError(f"{name} was already removed from the registry. Refusing to purge an unexpected "
                                     f"profile directory: its files were left untouched at {target}.")
                shutil.rmtree(resolved)
                purged = True
                kept = None
        return {"removed": name, "purged": purged, "kept": kept}
