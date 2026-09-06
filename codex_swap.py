#!/usr/bin/env python3
"""Account-scoped launchers for Codex CLI and the Codex/ChatGPT desktop app."""
from __future__ import annotations

import argparse
import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import time

from xswap_usage import UsageError, is_ok, normalize_limits, read_limits, short_line, usage_lines, window_label
from xswap_usage import warnings as usage_warnings
from xswap_display import resolve_lang
from xswap_live import LiveError, buckets_available, jwt_claims
from xswap_plugins import ensure_plugins
from xswap_credentials import CredentialError, read_auth
from xswap_upgrade import UpgradeError, upgrade
from xswap_alert import AlertError
from xswap_alert import install as alert_install, status as alert_status, uninstall as alert_uninstall

__version__ = "0.6.3"


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


def check_file_store(home):
    config = home / "config.toml"
    try:
        data = tomllib.loads(config.read_text()) if config.exists() else {}
    except (ValueError, OSError):
        raise SwapError(f"Cannot read valid config.toml at {home}") from None
    store = data.get("cli_auth_credentials_store", "file")
    if store != "file":
        raise SwapError(f"{home}: credential storage is {store!r}; this version supports file storage only. No credentials changed.")


def resolve_openclaw_package_root(executable):
    """Walk up from the openclaw executable to its package.json (name: "openclaw")."""
    for directory in Path(executable).resolve().parents:
        package = directory / "package.json"
        if package.is_file():
            try:
                if json.loads(package.read_text()).get("name") == "openclaw":
                    return directory
            except (OSError, ValueError):
                pass
    return None


def parse_pool(value):
    """Split "a, b,a" (or dedupe an already-split list) into >=2 distinct, ordered names."""
    parts = value.split(",") if isinstance(value, str) else value
    seen = []
    for part in parts:
        part = part.strip() if isinstance(part, str) else part
        if part and part not in seen:
            seen.append(part)
    if len(seen) < 2:
        raise SwapError("--pool needs at least two distinct registered account names.")
    return seen


def chatgpt_org_id(home, name):
    """Unverified org id from a local credential, for the same-organization pool guard.

    Checks access_token first, matching xswap_live.load_credentials' claim source; the
    id_token fallback is this guard's own extension (load_credentials has none), covering
    a credential whose access_token lacks the claim but whose id_token still carries it.
    Fails closed: raises SwapError instead of returning None/unknown, because an
    undeterminable organization must never be treated as matching another account's
    organization (that would silently let mismatched orgs share one pool).
    """
    try:
        data = read_auth(home)
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            for key in ("access_token", "id_token"):
                token = tokens.get(key)
                if isinstance(token, str) and token:
                    auth = jwt_claims(token).get("https://api.openai.com/auth")
                    if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
                        return auth["chatgpt_account_id"]
    except CredentialError:
        pass
    raise SwapError(f"cannot verify the ChatGPT organization for account {name}; run xswap login {name} or pass --allow-mixed.")


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


def codex_windows(buckets):
    return [window for bucket in buckets if bucket["id"] == "codex" for window in bucket["windows"]]


def window_percent(buckets, minutes):
    return next((w["remainingPercent"] for w in codex_windows(buckets) if w["windowMinutes"] == minutes), None)


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
            if (data.get("version") != 1 or not isinstance(data.get("accounts"), dict) or
                    ("mappings" in data and not isinstance(data["mappings"], dict))):
                raise ValueError()
            return data
        except (ValueError, OSError):
            raise SwapError("Invalid account registry; refusing to overwrite it.") from None

    def account(self, name=None, mapped=True):
        data = self.read()
        name = name or (self.default_account() if mapped else data["active"])
        if name not in data["accounts"]:
            # ASCII-only hint text: some terminals/log pipelines mangle non-ASCII dashes.
            hint = " -- no accounts yet -- run xswap init" if not data["accounts"] else ""
            raise SwapError(f"No matching account. Run: xswap register main, or xswap add NAME{hint}")
        return name, Path(data["accounts"][name]["home"])

    def _best_mapping(self, data, cwd=None):
        target_parts = Path(cwd or os.getcwd()).resolve().parts
        best = None  # (depth, path, name)
        for raw_path, name in data.get("mappings", {}).items():
            parts = Path(raw_path).parts
            if len(parts) <= len(target_parts) and tuple(parts) == target_parts[:len(parts)]:
                if best is None or len(parts) > best[0]:
                    best = (len(parts), raw_path, name)
        return best

    def resolve_default(self, cwd=None):
        """Return (name, mapping_path) for the implicit-selection account, computing the
        directory-mapping lookup exactly once. mapping_path is None when the active
        account (not a mapping) decided the result."""
        data = self.read()
        best = self._best_mapping(data, cwd)
        if best and best[2] in data["accounts"]:
            return best[2], best[1]
        return data["active"], None

    def default_account(self, cwd=None):
        return self.resolve_default(cwd)[0]

    def mapped_source(self, cwd=None):
        """Return the mapping path that decided default_account(cwd), or None."""
        return self.resolve_default(cwd)[1]

    def map_dir(self, name, path=None):
        resolved = str(Path(path or os.getcwd()).expanduser().resolve())
        with self.locked():
            name, _ = self.account(name)
            data = self.read()
            data.setdefault("mappings", {})[resolved] = name
            atomic_json(self.registry, data)
        return resolved, name

    def unmap_dir(self, path=None):
        resolved = str(Path(path or os.getcwd()).expanduser().resolve())
        with self.locked():
            data = self.read()
            mappings = data.get("mappings", {})
            if resolved not in mappings:
                raise SwapError(f"No mapping for {resolved}.")
            del mappings[resolved]
            data["mappings"] = mappings
            atomic_json(self.registry, data)
        return resolved

    def list_mappings(self):
        return dict(self.read().get("mappings", {}))

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
            self.require_enabled(name)
            check_file_store(home)
            if identity(home) in ("not signed in", "unreadable auth cache"):
                raise SwapError(f"Account {name} is not signed in. Run: xswap add {name}")
            data = self.read()
            data["active"] = name
            atomic_json(self.registry, data)
        return home

    def select_if_unset(self, name):
        # Mirrors use(): the None check, the signed-in check, and the write share one lock
        # so a concurrent add/use cannot race between "no active account" and setting one.
        with self.locked():
            name, home = self.account(name)
            self.require_enabled(name)
            check_file_store(home)
            if identity(home) in ("not signed in", "unreadable auth cache"):
                raise SwapError(f"Account {name} is not signed in. Run: xswap add {name}")
            data = self.read()
            if data["active"] is not None:
                return False
            data["active"] = name
            atomic_json(self.registry, data)
        return True

    def require_enabled(self, name):
        data = self.read()
        if data["accounts"][name].get("disabled"):
            raise SwapError(f"Account {name} is disabled. Run: xswap enable {name}")

    def enabled_accounts(self):
        data = self.read()
        return [(name, Path(value["home"])) for name, value in data["accounts"].items() if not value.get("disabled")]

    def set_disabled(self, name, disabled):
        with self.locked():
            name, _ = self.account(name)
            data = self.read()
            if disabled:
                data["accounts"][name]["disabled"] = True
            else:
                data["accounts"][name].pop("disabled", None)
            atomic_json(self.registry, data)

    def _auto_session_running(self, name):
        """Best effort: report whether a live auto bridge (desktop or CLI) currently uses this account."""
        candidates = [self.root / "auto" / ".bridge.lock"]
        cli_runs = self.root / "auto" / "cli-runs"
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
                    if isinstance(state, dict) and state.get("account") == name:
                        return True
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return False

    def remove(self, name, purge=False):
        from xswap_cli import read_settings
        with self.locked():
            name, home = self.account(name)
            settings = read_settings(self)
            if settings.get("enabled") and name in settings.get("accounts", []):
                raise SwapError(f"{name} is in the automatic switching pool. Run xswap auto-disable or auto-enable with a new pool first.")
            if self._auto_session_running(name):
                raise SwapError(f"{name} is used by a running auto session.")
            data = self.read()
            entry = data["accounts"].pop(name)
            if data["active"] == name:
                data["active"] = None
            atomic_json(self.registry, data)
            self._forget_usage(name)
            purged = False
            kept = entry["home"]
            if purge and entry.get("managed"):
                target = self.root / "profiles" / name
                resolved = target.resolve()
                if (resolved != target or resolved.is_symlink() or not resolved.is_dir()
                        or resolved.stat().st_uid != os.getuid()):
                    raise SwapError(f"{name} was already removed from the registry. Refusing to purge an unexpected "
                                     f"profile directory: its files were left untouched at {target}.")
                shutil.rmtree(resolved)
                purged = True
                kept = None
        return {"removed": name, "purged": purged, "kept": kept}

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

    def usage_cache_path(self):
        return self.root / "usage-cache.json"

    def cached_usage(self, name, label, max_age):
        """Return {"buckets", "fetchedAt"} from a still-fresh cache entry, or None.

        A saved entry whose identity label no longer matches the account's current
        login (a re-login under the same name) is treated as a miss, not reused.
        """
        if not max_age or max_age <= 0:
            return None
        try:
            data = json.loads(self.usage_cache_path().read_text())
        except (OSError, ValueError):
            return None
        entry = data.get(name) if isinstance(data, dict) else None
        if not isinstance(entry, dict) or entry.get("identity") != label:
            return None
        fetched_at, buckets = entry.get("fetchedAt"), entry.get("buckets")
        if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool) or not isinstance(buckets, list):
            return None
        if time.time() - fetched_at > max_age:
            return None
        return {"buckets": buckets, "fetchedAt": fetched_at}

    def _forget_usage(self, name):
        """Drop a removed account's cache entry (label may be an email). Caller holds the lock."""
        try:
            data = json.loads(self.usage_cache_path().read_text())
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.pop(name, None) is not None:
            atomic_json(self.usage_cache_path(), data)

    def remember_usage(self, name, buckets, fetched_at, label):
        """Whitelisted normalized fields only; never raw responses or tokens. Always 0600."""
        with self.locked():
            try:
                data = json.loads(self.usage_cache_path().read_text())
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[name] = {"buckets": buckets, "fetchedAt": fetched_at, "identity": label}
            atomic_json(self.usage_cache_path(), data)

    def account_usage(self, name, home, offline=False, disabled=False, max_age=None):
        label = identity(home)
        row = {"name": name, "identity": label, "status": "offline", "buckets": [], "fetchedAt": None, "disabled": disabled, "cached": False}
        if disabled:
            row["status"] = "disabled"
        elif label in ("not signed in", "unreadable auth cache"):
            row["status"] = label
        elif label == "API key":
            row["status"] = "API key: subscription quota not available"
        elif not offline:
            cached = self.cached_usage(name, label, max_age)
            if cached is not None:
                row.update(status="ok (cached)", buckets=cached["buckets"], fetchedAt=cached["fetchedAt"], cached=True)
            else:
                try:
                    check_file_store(home)
                    response = read_limits(self.codex(), self.env(home))
                    buckets, fetched_at = normalize_limits(response), time.time()
                    row.update(status="ok", buckets=buckets, fetchedAt=fetched_at)
                    self.remember_usage(name, buckets, fetched_at, label)
                except (UsageError, SwapError) as error:
                    row["status"] = f"usage unavailable: {error}"
                except OSError:
                    row["status"] = "usage unavailable: cannot start Codex CLI"
        return row

    def account_rows(self, name=None, offline=False, max_age=None):
        data = self.read()
        enabled_names = {n for n, _ in self.enabled_accounts()}
        if name is not None:
            selected, home = self.account(name)
            accounts = [(selected, home, selected not in enabled_names)]
        else:
            accounts = [(key, Path(value["home"]), key not in enabled_names) for key, value in data["accounts"].items()]
        # Every server has its own account home; no global authentication switch is needed.
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(accounts)))) as pool:
            rows = list(pool.map(lambda item: self.account_usage(item[0], item[1], offline=offline, disabled=item[2], max_age=max_age), accounts))
        for row in rows:
            row["active"] = row["name"] == data["active"]
        return rows

    def show_accounts(self, name=None, offline=False, json_output=False, include_spark=False, details=False, short=False, max_age=None, lang="en"):
        if short and json_output:
            raise SwapError("--short and --json are mutually exclusive.")
        rows = self.account_rows(name, offline, max_age)
        if short:
            # An explicit `usage NAME --short` always shows that one account, even if
            # disabled; the aggregate `list --short` omits disabled accounts instead.
            print(short_line(rows if name is not None else [r for r in rows if not r.get("disabled")]))
            return rows
        if json_output:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return rows
        from xswap_display import render
        from xswap_cli import read_settings
        print(render(rows, read_settings(self), include_spark, details, lang=lang))
        return rows

    def best_account(self, model=None, exclude=(), max_age=None):
        """Pick the signed-in account with the most codex headroom right now.

        A one-shot choice for launchers (like `codex exec`) that have no
        `--remote` hook and so cannot be protected by the live auto bridge.
        """
        excluded = set(exclude)
        # Reuse the single parallel-fetch implementation; filter out disabled/excluded rows.
        candidates = [row for row in self.account_rows(max_age=max_age) if not row["disabled"] and row["name"] not in excluded]
        if not candidates:
            return None, {"remaining": {}, "candidates": []}
        summary, ranked = [], []
        for row in candidates:
            summary.append({"name": row["name"], "remaining5h": window_percent(row["buckets"], 300),
                             "remaining7d": window_percent(row["buckets"], 10080), "status": row["status"]})
            if is_ok(row["status"]) and buckets_available(row["buckets"], model) is True:
                ranked.append(row)
        if not ranked:
            return None, {"remaining": {}, "candidates": summary}

        def rank_key(row):
            known = [w for w in codex_windows(row["buckets"]) if w["remainingPercent"] is not None]
            if not known:
                return (0, float("inf"))  # defensive: buckets_available(...) is True already guarantees this
            tightest = min(known, key=lambda w: w["remainingPercent"])
            return (-tightest["remainingPercent"], tightest["resetsAt"] if tightest["resetsAt"] is not None else float("inf"))

        winner = min(ranked, key=rank_key)
        remaining = {window_label(w): w["remainingPercent"] for w in codex_windows(winner["buckets"])}
        return winner["name"], {"remaining": remaining, "candidates": summary}

    def sync_openclaw(self, names=None, agents=None, dry=False, backup_dir=None, select=False, allow_mixed=False):
        # Directory mappings scope a single launched session; OpenClaw sync mutates
        # shared agent state, so an omitted/pooled name must resolve through the
        # active account only, never a directory mapping.
        pool = parse_pool(names) if isinstance(names, list) else [names]
        resolved = []
        for entry in pool:
            entry_name, entry_home = self.account(entry, mapped=False)
            self.require_enabled(entry_name)
            check_file_store(entry_home)
            resolved.append((entry_name, entry_home))
        if len(resolved) > 1 and not allow_mixed:
            groups = {}
            for entry_name, entry_home in resolved:
                org = chatgpt_org_id(entry_home, entry_name)
                groups.setdefault(org, []).append(entry_name)
            if len(groups) > 1:
                pretty = ", ".join("[" + ", ".join(names_in_group) + "]" for names_in_group in groups.values())
                raise SwapError(f"Pooled accounts belong to different ChatGPT organizations: {pretty}. OpenClaw shares one agent's conversation context across whatever it selects from the pool, so mixing organizations mixes their context across accounts. Pass --allow-mixed to override.")
        name, home = resolved[0]
        executable = shutil.which("openclaw")
        node = shutil.which("node")
        if not executable or not node:
            raise SwapError("OpenClaw sync requires openclaw and node in PATH.")
        package_root = resolve_openclaw_package_root(executable)
        if package_root is None:
            raise SwapError("Cannot locate OpenClaw's installed package through its executable. Use the standard npm installation.")
        helper = Path(__file__).resolve().parent / "xswap_bridge" / "openclaw.mjs"
        # Keep the single-account "account"/"codexHome" keys byte-compatible; "accounts" carries the full pool.
        request = {"account": name, "codexHome": str(home),
                   "accounts": [{"name": entry_name, "codexHome": str(entry_home)} for entry_name, entry_home in resolved],
                   "packageRoot": str(package_root), "agents": agents or [], "dryRun": dry,
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
        label = ", ".join(entry_name for entry_name, _ in resolved)
        rotates = " to rotate on its own cooldowns" if len(resolved) > 1 else ""
        print(f"OpenClaw now selects {label} for {len(output['completed'])} agents{rotates}; Gateway auth reloaded.\nBackup: {output['backup']}")
        return output

    def clear_openclaw_cooldown(self, names=None, dry=False, yes=False, backup_dir=None):
        """Clear a stale OpenClaw auth-profile cooldown (one 429 lasts until its reset
        time even after the real limit is gone) for the given account(s), or every
        registered account when none are named. Stops the local Gateway before writing
        and restarts it after; a failed stop aborts before the database is touched.
        """
        from xswap_openclaw_state import OpenClawStateError, clear_cooldown, default_sqlite_path, format_until, profile_id_for_home, read_cooldowns

        data = self.read()
        if names:
            pool = names if isinstance(names, list) else [names]
            entries = []
            for entry in pool:
                entry_name, entry_home = self.account(entry, mapped=False)
                entries.append((entry_name, entry_home))
        else:
            entries = [(name, Path(value["home"])) for name, value in data["accounts"].items()]
        if not entries:
            raise SwapError("No registered accounts to check.")

        sqlite_path = default_sqlite_path()
        if not sqlite_path.exists():
            raise SwapError(f"OpenClaw state database not found: {sqlite_path}")
        try:
            cooldowns = read_cooldowns(sqlite_path)
        except OpenClawStateError as exc:
            raise SwapError(str(exc)) from None

        targets = []  # [(name, profile_id)]
        for entry_name, entry_home in entries:
            profile_id = profile_id_for_home(entry_home)
            if profile_id and profile_id in cooldowns:
                targets.append((entry_name, profile_id))

        if not targets:
            print("No stale OpenClaw cooldowns found.")
            return {"cleared": []}

        for entry_name, profile_id in targets:
            info = cooldowns[profile_id]
            reason = info.get("blockedReason") or "unknown"
            print(f"{entry_name} ({profile_id}): blocked {format_until(info['blockedUntil'])} ({reason})")

        if dry:
            print("Dry run: no changes made; the Gateway was not stopped.")
            return {"cleared": [], "dryRun": True}

        executable = shutil.which("openclaw")
        if not executable:
            raise SwapError("OpenClaw sync requires openclaw in PATH.")

        if not yes:
            if not sys.stdin.isatty():
                raise SwapError("Confirm with --yes.")
            prompt = f"Stop the Gateway and clear {len(targets)} OpenClaw cooldown(s)? [y/N] "
            if input(prompt).strip().lower() != "y":
                print("Aborted.")
                return {"cleared": [], "aborted": True}
        backup_root = Path(backup_dir).expanduser().resolve() if backup_dir else self.root / "backups" / "openclaw-cooldown"

        with self.locked():
            stop = subprocess.run([executable, "gateway", "stop", "--force"],
                                  capture_output=True, text=True, env=self.env(self.source), timeout=45)
            if stop.returncode:
                raise SwapError("OpenClaw Gateway stop failed; the database was not touched. "
                                 f"{(stop.stderr or stop.stdout).strip() or 'unknown error'}")
            try:
                backup_path = clear_cooldown(sqlite_path, [pid for _, pid in targets], backup_root)
            except OpenClawStateError as exc:
                raise SwapError(f"Cooldown clear failed after the Gateway was stopped; restart it manually: "
                                 f"openclaw gateway start. {exc}") from None
            start = subprocess.run([executable, "gateway", "start"],
                                   capture_output=True, text=True, env=self.env(self.source), timeout=45)
            if start.returncode:
                raise SwapError(f"Cleared the cooldown (backup: {backup_path}), but OpenClaw Gateway start failed; "
                                 f"run: openclaw gateway start. {(start.stderr or start.stdout).strip() or 'unknown error'}")

        cleared = [entry_name for entry_name, _ in targets]
        print(f"Cleared {len(targets)} OpenClaw cooldown(s): {', '.join(cleared)}.\nBackup: {backup_path}")
        return {"cleared": cleared, "backup": str(backup_path)}

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
    st.add_argument("--prune", action="store_true", help="Remove non-running CLI run records now, not only ones older than 7 days")
    ae = sub.add_parser("auto-enable", help="Enable automatic switching for new xswap CLI/app sessions")
    ae.add_argument("--accounts", required=True)
    ae.add_argument("--wrap-codex", action="store_true", help="Also wrap the user-owned codex symlink, with rollback metadata")
    policy = sub.add_parser("auto-policy", help="Set weekly remaining percentage for proactive switching")
    policy.add_argument("--weekly-remaining", type=float, required=True)
    rp = sub.add_parser("repair-plugins", help="Materialize legacy shared plugin links without stopping sessions")
    rp.add_argument("--dry-run", action="store_true")
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
    al.add_argument("--every", type=float, default=30, metavar="MINUTES", help="Polling interval in minutes (default 30; launchd merges under 60s)")
    al.add_argument("--cached", type=float, default=600, metavar="SECONDS", help="Freshness passed to `list --cached` (default 600)")
    u = sub.add_parser("use", aliases=["switch"], help="Select the default account for xswap and xswap app")
    u.add_argument("name", nargs="?")
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
            from xswap_init import run_init
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
            # The account must be signed in before it can become active, so this only runs after login succeeds.
            if args.use:
                manager.use(args.name)
                print(f"Selected {args.name}.")
            elif manager.select_if_unset(args.name):
                print(f"Selected {args.name} (first account).")
        elif args.command == "login":
            return manager.login(args.name, args.device_auth)
        elif args.command == "list":
            threshold = validate_warn_threshold(args.warn) if args.warn is not None else None
            max_age = parse_cache_seconds(args.cached)
            if args.offline and max_age is not None:
                raise SwapError("--offline and --cached cannot be combined.")
            lang = resolve_lang(args.lang, os.environ)
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
            from xswap_display import dashboard
            lang = resolve_lang(args.lang, os.environ)
            print(json.dumps(dashboard(manager, lang=lang), ensure_ascii=False))
        elif args.command == "menubar":
            from xswap_menubar import launch
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
            from xswap_cli import set_policy
            set_policy(manager, args.weekly_remaining)
        elif args.command == "repair-plugins":
            from xswap_plugins import repair
            repair(manager, args.dry_run)
        elif args.command == "doctor":
            from xswap_doctor import run, print_report
            return print_report(run(manager), args.json_output)
        elif args.command == "auto-enable":
            from xswap_cli import enable
            enable(manager, args.accounts, args.wrap_codex)
        elif args.command == "auto-disable":
            from xswap_cli import disable
            disable(manager)
        elif args.command == "auto-status":
            from xswap_cli import show_status
            show_status(manager, args.prune)
        elif args.command == "upgrade":
            return upgrade(__version__, args.tag, args.dry_run)
        elif args.command == "alert":
            if sum((args.install, args.uninstall, args.status)) != 1:
                raise SwapError("Give exactly one of --install, --uninstall, or --status.")
            if args.install:
                return alert_install(manager.root, warn=args.warn, every=args.every, cached=args.cached, dry_run=args.dry_run)
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
                name = args.name
            if args.openclaw:
                manager.sync_openclaw(name, select=True)
            else:
                manager.use(name)
            print(f"Selected {name}{detail}. CLI: xswap · Desktop: xswap app")
            if not args.default_only:
                from xswap_switch import switch_running
                report = switch_running(manager, name)
                print('Running bridges: ' + ', '.join(f'{key}={value}' for key, value in report.items()))
                print('Pending requests apply when the current turn finishes. '
                      'Older bridges and sessions without a bridge require reopening once.')
                if report['failed'] or report['unconfirmed']:
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
            target = manager.root / "profiles" / name
            will_purge = args.purge and entry.get("managed")
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
            from xswap_cli import read_settings
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
            from xswap_completion import generate
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
                from xswap_cli import launch_cli
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
