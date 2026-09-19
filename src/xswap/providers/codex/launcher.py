"""Environment scrubbing, real-Codex resolution and the CLI/desktop launchers.

`after_login` is the one cross-concern sequence here: it drops the usage-cache
and auth-state entries for an account under ONE `Manager.locked()` hold, by
calling the facade's lock-free `_forget_*` helpers.

`subprocess`, `shutil`, `sys` and `os` are the very same module objects
`xswap.manager` holds, so the suite's `patch("xswap.manager.subprocess.call")`,
`patch("xswap.manager.shutil.which")` and `patch("xswap.manager.sys.platform")`
reach this code unchanged. `identity`, `check_file_store` and `private_dir` go
through `hooks`, which looks them up in `xswap.manager` at call time.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from xswap.core.errors import SwapError
from xswap.providers.codex.live import LiveError
from xswap.core.path import absolute_which, relative_entry_message
from xswap.core.paths import auto_dir, profile_dir
from xswap.providers.codex.plugins import ensure_plugins
from xswap.providers.codex.relocate import link_packages
from xswap.core.reports import describe_login_report


class Launcher:
    def __init__(self, manager, hooks):
        self.manager = manager
        self.hooks = hooks

    def env(self, home):
        env = os.environ.copy()
        # A caller's API key or workload identity must not silently select another account.
        # XSWAP_BYPASS with them: exported in the shell that started this launch, it followed the
        # pool account's home into every nested `codex` and turned the wrapper off inside the one
        # session xswap had just set up -- with this home already on CODEX_HOME, that is a session
        # whose selection nothing can change. One command's own bypass stays that command's.
        # The endpoint and trust variables below are the ones the Codex CLI itself reads: left in
        # place, a value exported in the calling shell decided where the account xswap had just
        # selected sent its refresh token (CODEX_REFRESH_TOKEN_URL_OVERRIDE,
        # CODEX_REVOKE_TOKEN_URL_OVERRIDE), which backend answered for its usage and plan
        # (CODEX_APP_SERVER_CHATGPT_BASE_URL, OPENAI_BASE_URL), which CA chain that traffic was
        # checked against (CODEX_CA_CERTIFICATE), and where its conversation database lived
        # (CODEX_SQLITE_HOME, the one home-shaped variable CODEX_HOME does not cover). The
        # federation set is the OPENAI_ spelling of the workload identity already stripped here:
        # an exchanged token authenticates a session as something other than the login in this
        # home, which is the selection silently not applying.
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_ELECTRON_USER_DATA_PATH",
                    "XSWAP_BYPASS", "CODEX_APP_SERVER_CHATGPT_BASE_URL", "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
                    "CODEX_REVOKE_TOKEN_URL_OVERRIDE", "CODEX_CA_CERTIFICATE", "CODEX_SQLITE_HOME",
                    "OPENAI_BASE_URL", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_FEDERATION_RULE_ID"):
            env.pop(key, None)
        for key in list(env):
            if key.startswith(("CODEX_WORKLOAD_IDENTITY_", "OPENAI_WORKLOAD_IDENTITY_")):
                env.pop(key)
        env["CODEX_HOME"] = str(home)
        return env

    def codex(self):
        executable = shutil.which("codex")
        if not executable:
            raise SwapError("codex is not installed or not in PATH.")
        from xswap.providers.codex.codex_cli import dependency_entry, read_settings, reconnect_wrapper, recorded_real_codex, wrapped_target
        reconnect_wrapper(self.manager)
        settings = read_settings(self.manager)
        wrapper = settings.get("wrapper")
        # Any xswap-codex, not only the recorded proxy: a second install ahead on PATH (a project
        # venv, pipx beside uv) was returned as the real Codex, so every launch exec'd xswap-codex,
        # which exec'd itself. Same widening for the recorded realCodex, which is how a record
        # written before this fix spells that loop.
        if wrapper and (Path(executable).resolve() == Path(wrapper["proxy"]).resolve()
                        or wrapped_target(executable, settings)):
            recorded = wrapper["realCodex"]
            # recorded_real_codex refuses a value that is not absolute: 0.7.8 wrote one whenever
            # the entry it wrapped was relative, and resolving it here execs whatever `codex` the
            # working directory holds -- with a pool account's CODEX_HOME.
            real = recorded_real_codex(settings)
            if not real or not Path(real).is_file() or wrapped_target(real, settings):
                names = ",".join(name for name, _ in self.manager.enabled_accounts()) or "NAME,NAME"
                where = (f"{recorded} is not an absolute path" if real is None and recorded
                         else f"{recorded} is missing or is xswap-codex itself" if recorded
                         else "auto.json records no realCodex")
                raise SwapError(f"Original Codex binary is unavailable ({where}). "
                                f"Recover: xswap auto-disable, reinstall Codex, then xswap auto-enable --accounts {names} --wrap-codex")
            return real
        # shutil.which joins the raw PATH element, so a relative one (a project's `bin`, the empty
        # element POSIX reads as the working directory) returns a relative path: the `codex` of
        # whatever directory xswap happens to run in. Every caller execs this with a pool account's
        # CODEX_HOME, which would hand a project-controlled file that account's auth.json, and the
        # entry xswap actually wrapped would be ignored. It is the entry `auto-enable --wrap-codex`
        # refuses and doctor reports as bypassed, so run the recorded release instead of it.
        if not os.path.isabs(executable):
            real = recorded_real_codex(settings)
            if real and Path(real).is_file():
                return real
            raise SwapError(relative_entry_message("codex", executable))
        # The same one step out: npm writes `node_modules/.bin/codex` for a project that depends
        # on @openai/codex, and that directory reaches PATH absolutely (direnv, a Makefile, an IDE
        # task). Exec'ing it hands a project-controlled file a pool account's CODEX_HOME. xswap
        # never adopts such an entry (DEPENDENCY_REASON), so run the recorded release instead.
        if dependency_entry(executable, os.path.realpath(executable)):
            real = recorded_real_codex(settings)
            if real and Path(real).is_file():
                return real
        return executable

    def login(self, name, device_auth=False, lang="en"):
        name, home = self.manager.account(name)
        self.hooks.check_file_store(home)
        command = [self.manager.codex(), "login"] + (["--device-auth"] if device_auth else [])
        result = subprocess.call(command, env=self.manager.env(home))
        if not result:
            print(f"Signed in {name}: {self.hooks.identity(home)}")
            self.manager.after_login(name, home, lang=lang)
        return result

    def after_login(self, name, home, lang="en"):
        """A (re-)login changes what the usage service says and what running
        bridges hold. Drop the cached quota entry and read it live once, then
        ask bridges already on this account to re-authenticate in place.
        Sessions on other accounts are untouched: they read the home again the
        next time they consider it. Bridges before 0.7.2 need reopening once."""
        from xswap.core.display import summary
        from xswap.providers.codex.switch import switch_running
        # One lock hold spanning two stores, exactly as before the split: both
        # `_forget_*` helpers assume the caller holds it and take none themselves.
        with self.manager.locked():
            self.manager._forget_usage(name)
            self.manager._forget_auth_failure(name)
        row = self.manager.account_usage(name, home)
        if row["status"] == "ok":
            item = summary(row, lang=lang)
            print(f"{name} weekly: {item['summary']}" + (f" · {item['reset']}" if item['reset'] else ""))
        else:
            print(f"{name} usage: {row['status']}")
        report = switch_running(self.manager, name, only_current=True)
        print(describe_login_report(report, name))

    def launch_cli(self, name, args, dry=False):
        from xswap.providers.codex.codex_cli import read_settings, interactive_args, launch_cli
        settings = read_settings(self.manager)
        if name is None and settings.get("enabled") and interactive_args(args):
            return launch_cli(self.manager, ','.join(settings["accounts"]), args, dry)
        name, home = self.manager.account(name)
        self.hooks.check_file_store(home)
        command = [self.manager.codex(), *args]
        if dry:
            print(json.dumps({"account": name, "CODEX_HOME": str(home), "argv": command}, indent=2))
            return 0
        ensure_plugins(home, self.manager.source)
        link_packages(self.manager, home)  # no-op for a registered external home
        try:
            return subprocess.call(command, env=self.manager.env(home))
        finally:
            # `upgrade`/`update` reach this branch (they are non-interactive), and Codex's
            # standalone updater re-points the wrapped `codex` entry on its way out. xswap
            # still holds this process, so repair the entry here the way launch_cli's own
            # exit path does; without it `xswap run -- upgrade` left plain `codex` running
            # against the caller's home -- the 2026-09-10 bypass, re-created by xswap.
            with contextlib.suppress(LiveError, OSError, SwapError):
                from xswap.providers.codex.codex_cli import reconnect_wrapper
                reconnect_wrapper(self.manager)

    def launch_auto_app(self, accounts, app=None, dry=False):
        from xswap.providers.codex.live import AccountPool, LiveError
        names = [value.strip() for value in (accounts or '').split(',') if value.strip()]
        real = self.manager.codex()
        try:
            AccountPool(self.manager, names, real)
        except LiveError as exc:
            raise SwapError(str(exc)) from None
        if sys.platform != "darwin":
            raise SwapError("Auto desktop mode is macOS only.")
        # A relative hit was absolutised against the working directory by Path(bridge).resolve()
        # below and handed to the desktop app as CODEX_CLI_PATH: the app then ran a file the
        # current project controls as its Codex CLI, for the whole life of that window.
        bridge = absolute_which("xswap-proxy", error=SwapError)
        if not bridge:
            raise SwapError("Install xswap 0.3.0 to provide xswap-proxy.")
        candidates = [Path(app)] if app else [Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app")]
        bundle = next((path for path in candidates if path.is_dir()), None)
        if not bundle:
            raise SwapError("Desktop app not found; use --app /path/to/ChatGPT.app")
        root = auto_dir(self.manager.root)
        home, desktop = root / "codex", root / "desktop"
        env_values = {"CODEX_HOME": str(home), "CODEX_ELECTRON_USER_DATA_PATH": str(desktop),
                      "CODEX_CLI_PATH": str(Path(bridge).resolve()), "CODEX_APP_SERVER_FORCE_CLI": "1",
                      "XSWAP_REAL_CODEX": str(Path(real).resolve()), "XSWAP_ACCOUNTS": ','.join(names),
                      "CODEX_SWAP_HOME": str(self.manager.root)}
        command = ["/usr/bin/open", "-n"]
        for key, value in env_values.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend([str(bundle), "--args", f"--user-data-dir={desktop}"])
        if dry:
            print(json.dumps({"mode": "auto", "accounts": names, "argv": command}, indent=2))
            return 0
        for path in (root, home, desktop):
            self.hooks.private_dir(path)
        _, source = self.manager.account(names[0])
        # Shared tooling; the auto window owns its own conversation store. Auth stays in source homes.
        for entry in ("config.toml", "AGENTS.md", "skills", "rules"):
            src, dst = source / entry, home / entry
            if src.exists() and not dst.exists() and not dst.is_symlink():
                # Two launches share this runtime home and neither holds the registry lock here,
                # so the other one can create the same link between the test and the call. The
                # link it created is the link this one was about to create, so the loser has
                # nothing to do -- but the FileExistsError reached codex_main, which reports
                # every OSError as "could not start automatic Codex CLI" and advises
                # `xswap auto-disable`, i.e. tearing the wrapper down over a won race.
                with contextlib.suppress(FileExistsError):
                    dst.symlink_to(src, target_is_directory=src.is_dir())
        ensure_plugins(home, source)
        link_packages(self.manager, home)
        result = subprocess.run(command, env=self.manager.env(home), capture_output=True)
        if result.returncode:
            raise SwapError("Auto desktop launch failed.")
        print("Opened auto-mode desktop: " + " -> ".join(names) +
              ". This window keeps its threads across account changes. Existing windows are unchanged.")
        return 0

    def launch_app(self, name, app=None, dry=False):
        name, home = self.manager.account(name)
        self.hooks.check_file_store(home)
        if sys.platform != "darwin":
            raise SwapError("Desktop launcher currently supports macOS only.")
        candidates = [Path(app)] if app else [Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app")]
        bundle = next((p for p in candidates if p.is_dir()), None)
        if not bundle:
            raise SwapError("Desktop app not found; specify --app /path/to/ChatGPT.app")
        desktop = profile_dir(self.manager.root, name) / "desktop"
        command = ["/usr/bin/open", "-n", "--env", f"CODEX_HOME={home}", "--env", f"CODEX_ELECTRON_USER_DATA_PATH={desktop}", str(bundle), "--args", f"--user-data-dir={desktop}"]
        if dry:
            print(json.dumps({"account": name, "argv": command}, indent=2))
            return 0
        self.hooks.private_dir(desktop.parent)
        self.hooks.private_dir(desktop)
        ensure_plugins(home, self.manager.source)
        link_packages(self.manager, home)  # no-op for a registered external home
        result = subprocess.run(command, env=self.manager.env(home), capture_output=True)
        if result.returncode:
            raise SwapError("Desktop launch failed. Confirm that the app path exists and macOS permits launching it.")
        print(f"Opened {name}. Existing windows keep their own account; verify this window's profile menu.")
        return 0
