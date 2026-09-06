#!/usr/bin/env python3
"""Account-scoped launchers for Codex CLI and the Codex/ChatGPT desktop app."""
from __future__ import annotations

import argparse
import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import time

from xswap_usage import UsageError, normalize_limits, read_limits, usage_lines
from xswap_live import LiveError
from xswap_plugins import ensure_plugins
from xswap_credentials import CredentialError, read_auth
from xswap_upgrade import UpgradeError, upgrade

__version__ = "0.4.3"


class SwapError(Exception):
    pass


def private_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise SwapError(f"Unsafe storage directory: {path}")
    path.chmod(0o700)


def atomic_json(path: Path, data):
    fd, tmp = tempfile.mkstemp(prefix=".swap-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_name(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", name):
        raise SwapError("Name must be 1–40 letters, digits, underscores or hyphens.")
    return name


def check_file_store(home):
    config = home / "config.toml"
    try:
        data = tomllib.loads(config.read_text()) if config.exists() else {}
    except (ValueError, OSError):
        raise SwapError(f"Cannot read valid config.toml at {home}") from None
    store = data.get("cli_auth_credentials_store", "file")
    if store != "file":
        raise SwapError(f"{home}: credential storage is {store!r}; this version supports file storage only. No credentials changed.")


def identity(home):
    path = home / "auth.json"
    if not path.exists():
        return "not signed in"
    try:
        data = read_auth(home)
        if not isinstance(data, dict):
            return "unreadable auth cache"
        if data.get("auth_mode") == "apikey" or data.get("OPENAI_API_KEY"):
            return "API key"
        tokens = data.get("tokens") or {}
        if not isinstance(tokens, dict):
            return "unreadable auth cache"
        token = tokens.get("id_token", "")
        if not isinstance(token, str):
            return "unreadable auth cache"
        parts = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))) if len(parts) == 3 else {}
        # These are unverified local labels, not evidence that the login is valid.
        label = (claims.get("email") or "ChatGPT") if isinstance(claims, dict) else "ChatGPT"
        return "".join(c for c in str(label) if c.isprintable()) if tokens.get("access_token") else "not signed in"
    except CredentialError as exc:
        raise SwapError(str(exc)) from None
    except (ValueError, OSError, IndexError, TypeError):
        return "unreadable auth cache"


class Manager:
    def __init__(self, root=None, source=None):
        self.root = Path(root or os.environ.get("CODEX_SWAP_HOME", Path.home() / ".local/share/codex-swap")).expanduser().resolve()
        self.source = Path(source or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        private_dir(self.root)
        self.registry = self.root / "accounts.json"

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self):
        if not self.registry.exists():
            return {"version": 1, "active": None, "accounts": {}}
        try:
            data = json.loads(self.registry.read_text())
            if data.get("version") != 1 or not isinstance(data.get("accounts"), dict):
                raise ValueError()
            return data
        except (ValueError, OSError):
            raise SwapError("Invalid account registry; refusing to overwrite it.") from None

    def account(self, name=None):
        data = self.read()
        name = name or data["active"]
        if name not in data["accounts"]:
            raise SwapError("No matching account. Run: xswap register main, or xswap add NAME")
        return name, Path(data["accounts"][name]["home"])

    def register(self, name, home=None):
        validate_name(name)
        home = Path(home or self.source).expanduser().resolve()
        check_file_store(home)
        if identity(home) in ("not signed in", "unreadable auth cache"):
            raise SwapError(f"No readable login at {home}. Use xswap add {name} to sign in.")
        with self.locked():
            data = self.read()
            if name in data["accounts"]:
                if data["accounts"][name]["home"] == str(home):
                    return home
                raise SwapError(f"Account {name!r} already exists.")
            if any(a["home"] == str(home) for a in data["accounts"].values()):
                raise SwapError("This Codex home is already registered under another name.")
            data["accounts"][name] = {"home": str(home), "managed": False}
            data["active"] = data["active"] or name
            atomic_json(self.registry, data)
        return home

    def prepare(self, name):
        validate_name(name)
        check_file_store(self.source)
        with self.locked():
            data = self.read()
            if name in data["accounts"]:
                return Path(data["accounts"][name]["home"])
            home = self.root / "profiles" / name / "codex"
            private_dir(home.parent)
            private_dir(home)
            # Share tooling, but never credentials, sessions, databases or app cookies.
            for entry in ("config.toml", "AGENTS.md", "skills", "rules"):
                src, dst = self.source / entry, home / entry
                if src.exists() and not dst.exists() and not dst.is_symlink():
                    dst.symlink_to(src, target_is_directory=src.is_dir())
            ensure_plugins(home, self.source)
            data["accounts"][name] = {"home": str(home), "managed": True}
            atomic_json(self.registry, data)
        return home

    def use(self, name):
        with self.locked():
            name, home = self.account(name)
            check_file_store(home)
            if identity(home) in ("not signed in", "unreadable auth cache"):
                raise SwapError(f"Account {name} is not signed in. Run: xswap add {name}")
            data = self.read()
            data["active"] = name
            atomic_json(self.registry, data)
        return home

    def env(self, home):
        env = os.environ.copy()
        # A caller's API key or workload identity must not silently select another account.
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_ELECTRON_USER_DATA_PATH"):
            env.pop(key, None)
        for key in list(env):
            if key.startswith("CODEX_WORKLOAD_IDENTITY_"):
                env.pop(key)
        env["CODEX_HOME"] = str(home)
        return env

    def codex(self):
        executable = shutil.which("codex")
        if not executable:
            raise SwapError("codex is not installed or not in PATH.")
        from xswap_cli import read_settings
        wrapper = read_settings(self).get("wrapper")
        if wrapper and Path(executable).resolve() == Path(wrapper["proxy"]).resolve():
            real = wrapper["realCodex"]
            if not Path(real).is_file() or Path(real).resolve() == Path(executable).resolve():
                raise SwapError("Original Codex binary is unavailable.")
            return real
        return executable

    def account_usage(self, name, home, offline=False):
        label = identity(home)
        row = {"name": name, "identity": label, "status": "offline", "buckets": [], "fetchedAt": None}
        if label in ("not signed in", "unreadable auth cache"):
            row["status"] = label
        elif label == "API key":
            row["status"] = "API key: subscription quota not available"
        elif not offline:
            try:
                check_file_store(home)
                response = read_limits(self.codex(), self.env(home))
                row.update(status="ok", buckets=normalize_limits(response), fetchedAt=time.time())
            except (UsageError, SwapError) as error:
                row["status"] = f"usage unavailable: {error}"
            except OSError:
                row["status"] = "usage unavailable: cannot start Codex CLI"
        return row

    def show_accounts(self, name=None, offline=False, json_output=False, include_spark=False):
        data = self.read()
        if name is not None:
            selected, home = self.account(name)
            accounts = [(selected, home)]
        else:
            accounts = [(key, Path(value["home"])) for key, value in data["accounts"].items()]
        # Every server has its own account home; no global authentication switch is needed.
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(accounts)))) as pool:
            rows = list(pool.map(lambda item: self.account_usage(*item, offline=offline), accounts))
        for row in rows:
            row["active"] = row["name"] == data["active"]
        if json_output:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        if not rows:
            print("No accounts. Start: xswap register main")
        for row in rows:
            plan = next((bucket["plan"] for bucket in row["buckets"] if bucket["plan"]), None)
            print(f"{'*' if row['active'] else ' '} {row['name']:16} {row['identity']}" + (f" [{plan}]" if plan else ""))
            if row["status"] == "ok":
                buckets = row["buckets"] if include_spark else [
                    bucket for bucket in row["buckets"]
                    if "spark" not in (bucket["id"] + " " + bucket["name"]).lower()]
                for line in usage_lines(buckets):
                    print(f"    {line}")
            elif row["status"] not in ("offline", "not signed in", "unreadable auth cache"):
                print(f"    {row['status']}")

    def sync_openclaw(self, name=None, agents=None, dry=False, backup_dir=None, select=False):
        name, home = self.account(name)
        check_file_store(home)
        executable = shutil.which("openclaw")
        node = shutil.which("node")
        if not executable or not node:
            raise SwapError("OpenClaw sync requires openclaw and node in PATH.")
        package_root = None
        for directory in Path(executable).resolve().parents:
            package = directory / "package.json"
            if package.is_file():
                try:
                    if json.loads(package.read_text()).get("name") == "openclaw":
                        package_root = directory
                        break
                except (OSError, ValueError):
                    pass
        if package_root is None:
            raise SwapError("Cannot locate OpenClaw's installed package through its executable. Use the standard npm installation.")
        helper = Path(__file__).resolve().parent / "xswap_bridge" / "openclaw.mjs"
        request = {"account": name, "codexHome": str(home), "packageRoot": str(package_root),
                   "agents": agents or [], "dryRun": dry,
                   "backupRoot": str(Path(backup_dir).expanduser().resolve() if backup_dir else self.root / "backups" / "openclaw")}
        # Serialize xswap mutations across the bridge + Gateway reload. OpenClaw also locks its own stores.
        with self.locked():
            result = subprocess.run([node, str(helper)], input=json.dumps(request), text=True,
                                    capture_output=True, env=self.env(home), timeout=90)
            try:
                output = json.loads(result.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise SwapError("OpenClaw SDK bridge failed. Check the installed OpenClaw version (tested: 2026.8.1).") from None
            if result.returncode or output.get("error"):
                raise SwapError(output.get("error", "OpenClaw credential update failed."))
            if dry:
                print(json.dumps(output, indent=2))
                return output
            reload_result = subprocess.run([executable, "secrets", "reload", "--json"],
                                           capture_output=True, text=True, env=self.env(home), timeout=45)
            try:
                reload_json = json.loads(reload_result.stdout)
            except ValueError:
                reload_json = {}
            if reload_result.returncode or reload_json.get("ok") is not True:
                raise SwapError(f"OpenClaw auth was saved for {len(output['completed'])} agents, but Gateway reload failed. Start/check the local Gateway, then run: openclaw secrets reload. Backup: {output['backup']}")
            if reload_json.get("warningCount", 0):
                raise SwapError("OpenClaw auth was saved and reloaded with warnings. Run openclaw secrets reload --json and inspect them before continuing.")
            if select:
                data = self.read()
                data["active"] = name
                atomic_json(self.registry, data)
        print(f"OpenClaw now selects {name} for {len(output['completed'])} agents; Gateway auth reloaded.\nBackup: {output['backup']}")
        return output

    def login(self, name, device_auth=False):
        name, home = self.account(name)
        check_file_store(home)
        command = [self.codex(), "login"] + (["--device-auth"] if device_auth else [])
        result = subprocess.call(command, env=self.env(home))
        if not result:
            print(f"Signed in {name}: {identity(home)}")
        return result

    def launch_cli(self, name, args, dry=False):
        from xswap_cli import read_settings, interactive_args, launch_cli
        settings = read_settings(self)
        if name is None and settings.get("enabled") and interactive_args(args):
            return launch_cli(self, ','.join(settings["accounts"]), args, dry)
        name, home = self.account(name)
        check_file_store(home)
        command = [self.codex(), *args]
        if dry:
            print(json.dumps({"account": name, "CODEX_HOME": str(home), "argv": command}, indent=2))
            return 0
        ensure_plugins(home, self.source)
        return subprocess.call(command, env=self.env(home))

    def launch_auto_app(self, accounts, app=None, dry=False):
        from xswap_live import AccountPool, LiveError
        names = [value.strip() for value in (accounts or '').split(',') if value.strip()]
        real = self.codex()
        try:
            AccountPool(self, names, real)
        except LiveError as exc:
            raise SwapError(str(exc)) from None
        if sys.platform != "darwin":
            raise SwapError("Auto desktop mode is macOS only.")
        bridge = shutil.which("xswap-proxy")
        if not bridge:
            raise SwapError("Install xswap 0.3.0 to provide xswap-proxy.")
        candidates = [Path(app)] if app else [Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app")]
        bundle = next((path for path in candidates if path.is_dir()), None)
        if not bundle:
            raise SwapError("Desktop app not found; use --app /path/to/ChatGPT.app")
        root = self.root / "auto"
        home, desktop = root / "codex", root / "desktop"
        env_values = {"CODEX_HOME": str(home), "CODEX_ELECTRON_USER_DATA_PATH": str(desktop),
                      "CODEX_CLI_PATH": str(Path(bridge).resolve()), "CODEX_APP_SERVER_FORCE_CLI": "1",
                      "XSWAP_REAL_CODEX": str(Path(real).resolve()), "XSWAP_ACCOUNTS": ','.join(names),
                      "CODEX_SWAP_HOME": str(self.root)}
        command = ["/usr/bin/open", "-n"]
        for key, value in env_values.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend([str(bundle), "--args", f"--user-data-dir={desktop}"])
        if dry:
            print(json.dumps({"mode": "auto", "accounts": names, "argv": command}, indent=2))
            return 0
        for path in (root, home, desktop):
            private_dir(path)
        _, source = self.account(names[0])
        # Shared tooling; the auto window owns its own conversation store. Auth stays in source homes.
        for entry in ("config.toml", "AGENTS.md", "skills", "rules"):
            src, dst = source / entry, home / entry
            if src.exists() and not dst.exists() and not dst.is_symlink():
                dst.symlink_to(src, target_is_directory=src.is_dir())
        ensure_plugins(home, source)
        result = subprocess.run(command, env=self.env(home), capture_output=True)
        if result.returncode:
            raise SwapError("Auto desktop launch failed.")
        print("Opened auto-mode desktop: " + " -> ".join(names) +
              ". This window keeps its threads across account changes. Existing windows are unchanged.")
        return 0

    def launch_app(self, name, app=None, dry=False):
        name, home = self.account(name)
        check_file_store(home)
        if sys.platform != "darwin":
            raise SwapError("Desktop launcher currently supports macOS only.")
        candidates = [Path(app)] if app else [Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app")]
        bundle = next((p for p in candidates if p.is_dir()), None)
        if not bundle:
            raise SwapError("Desktop app not found; specify --app /path/to/ChatGPT.app")
        desktop = self.root / "profiles" / name / "desktop"
        command = ["/usr/bin/open", "-n", "--env", f"CODEX_HOME={home}", "--env", f"CODEX_ELECTRON_USER_DATA_PATH={desktop}", str(bundle), "--args", f"--user-data-dir={desktop}"]
        if dry:
            print(json.dumps({"account": name, "argv": command}, indent=2))
            return 0
        private_dir(desktop.parent)
        private_dir(desktop)
        ensure_plugins(home, self.source)
        result = subprocess.run(command, env=self.env(home), capture_output=True)
        if result.returncode:
            raise SwapError("Desktop launch failed. Confirm that the app path exists and macOS permits launching it.")
        print(f"Opened {name}. Existing windows keep their own account; verify this window's profile menu.")
        return 0


def parser():
    p = argparse.ArgumentParser(description="Codex account switcher for CLI + macOS desktop. Bare xswap opens the selected CLI account.")
    p.add_argument("--version", action="version", version=f"xswap {__version__}")
    sub = p.add_subparsers(dest="command")
    r = sub.add_parser("register", help="Register an existing signed-in Codex home without copying its tokens")
    r.add_argument("name"); r.add_argument("--home", type=Path)
    a = sub.add_parser("add", help="Sign in to an isolated account home")
    a.add_argument("name"); a.add_argument("--device-auth", action="store_true")
    a.add_argument("--prepare-only", action="store_true")
    lg = sub.add_parser("login", help="Re-authenticate a registered account whose login expired")
    lg.add_argument("name"); lg.add_argument("--device-auth", action="store_true")
    listing = sub.add_parser("list", help="List accounts with live remaining quotas and reset times")
    listing.add_argument("--offline", action="store_true", help="Show local account labels without fetching usage")
    listing.add_argument("--json", action="store_true", dest="json_output")
    usage = sub.add_parser("usage", help="Show live quota windows for the selected or named account")
    listing.add_argument("--include-spark", action="store_true", help="Include Spark quotas in text output")
    usage.add_argument("--include-spark", action="store_true", help="Include Spark quotas in text output")
    usage.add_argument("name", nargs="?")
    usage.add_argument("--json", action="store_true", dest="json_output")
    sub.add_parser("status", help="Show selected account and login status")
    sub.add_parser("auto-status", help="Show desktop/CLI automatic switching state (no credentials)")
    ae = sub.add_parser("auto-enable", help="Enable automatic switching for new xswap CLI/app sessions")
    ae.add_argument("--accounts", required=True)
    ae.add_argument("--wrap-codex", action="store_true", help="Also wrap the user-owned codex symlink, with rollback metadata")
    policy = sub.add_parser("auto-policy", help="Set weekly remaining percentage for proactive switching")
    policy.add_argument("--weekly-remaining", type=float, required=True)
    rp = sub.add_parser("repair-plugins", help="Materialize legacy shared plugin links without stopping sessions")
    rp.add_argument("--dry-run", action="store_true")
    sub.add_parser("auto-disable", help="Disable auto defaults and restore the codex symlink")
    up = sub.add_parser("upgrade", help="Reinstall xswap from the latest (or a chosen) released Git tag")
    up.add_argument("--tag", help="Install this tag instead of the latest release, e.g. v0.5.0")
    up.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("use", help="Select the default account for xswap and xswap app")
    u.add_argument("name")
    u.add_argument("--openclaw", action="store_true", help="Also update all local OpenClaw agents and reload Gateway auth")
    o = sub.add_parser("openclaw", help="Sync an account to local OpenClaw agents and reload Gateway auth")
    o.add_argument("name", nargs="?")
    o.add_argument("--agent", action="append", dest="agents", help="Target only this agent (repeatable; default: all)")
    o.add_argument("--dry-run", action="store_true")
    o.add_argument("--backup-dir", type=Path, help="Directory for small, private auth-state backups")
    r = sub.add_parser("run", help="Run Codex with the selected or named account")
    r.add_argument("--account"); r.add_argument("--dry-run", action="store_true")
    r.add_argument("--auto", action="store_true", help="Keep the interactive CLI session alive across quota account switches")
    r.add_argument("--accounts", help="Explicit automatic fallback order")
    r.add_argument("args", nargs=argparse.REMAINDER)
    a = sub.add_parser("app", help="Open an account-specific desktop instance")
    a.add_argument("name", nargs="?"); a.add_argument("--app"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("--auto", action="store_true", help="Keep one desktop session and switch accounts after quota exhaustion (experimental)")
    a.add_argument("--accounts", help="Explicit fallback order, e.g. work,main; requires --auto")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        manager = Manager()
        if args.command == "register":
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
            print(f"Saved {args.name}: {identity(home)}. Select it: xswap use {args.name}")
        elif args.command == "login":
            return manager.login(args.name, args.device_auth)
        elif args.command == "list":
            manager.show_accounts(offline=args.offline, json_output=args.json_output, include_spark=args.include_spark)
        elif args.command == "usage":
            name, _ = manager.account(args.name)
            manager.show_accounts(name=name, json_output=args.json_output, include_spark=args.include_spark)
        elif args.command == "status":
            name, home = manager.account()
            print(f"Selected: {name}\nHome: {home}\nLocal label: {identity(home)}", flush=True)
            return manager.launch_cli(name, ["login", "status"])
        elif args.command == "auto-policy":
            from xswap_cli import set_policy
            set_policy(manager, args.weekly_remaining)
        elif args.command == "repair-plugins":
            from xswap_plugins import repair
            repair(manager, args.dry_run)
        elif args.command == "auto-enable":
            from xswap_cli import enable
            enable(manager, args.accounts, args.wrap_codex)
        elif args.command == "auto-disable":
            from xswap_cli import disable
            disable(manager)
        elif args.command == "auto-status":
            from xswap_cli import show_status
            show_status(manager)
        elif args.command == "upgrade":
            return upgrade(__version__, args.tag, args.dry_run)
        elif args.command == "use":
            if args.openclaw:
                manager.sync_openclaw(args.name, select=True)
            else:
                manager.use(args.name)
            print(f"Selected {args.name}. CLI: xswap · Desktop: xswap app\nRunning sessions and plain codex keep their current account.")
        elif args.command == "app":
            if args.auto:
                if args.name:
                    raise SwapError("Use --accounts instead of a positional name with --auto.")
                return manager.launch_auto_app(args.accounts, args.app, args.dry_run)
            if args.accounts:
                raise SwapError("--accounts requires --auto.")
            from xswap_cli import read_settings
            settings = read_settings(manager)
            if args.name is None and settings.get("enabled"):
                return manager.launch_auto_app(','.join(settings["accounts"]), args.app, args.dry_run)
            return manager.launch_app(args.name, args.app, args.dry_run)
        elif args.command == "openclaw":
            manager.sync_openclaw(args.name, args.agents, args.dry_run, args.backup_dir)
        else:
            rest = getattr(args, "args", [])
            if rest[:1] == ["--"]:
                rest = rest[1:]
            if getattr(args, "auto", False):
                if args.account or not args.accounts:
                    raise SwapError("Use --auto --accounts first,second without --account.")
                from xswap_cli import launch_cli
                return launch_cli(manager, args.accounts, rest, args.dry_run)
            if getattr(args, "accounts", None):
                raise SwapError("--accounts requires --auto.")
            return manager.launch_cli(getattr(args, "account", None), rest, getattr(args, "dry_run", False))
        return 0
    except (SwapError, LiveError, UpgradeError, OSError) as exc:
        print(f"xswap: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except subprocess.TimeoutExpired:
        print("xswap: Operation timed out; OpenClaw may be partially updated. Check xswap's auth backups and retry the same command. No existing sessions were stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
