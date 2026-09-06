"""Read-only diagnostics: codex binary, wrapper, credential store, accounts, auto pool, OpenClaw, storage.

No mutations, no network, and no secrets in output: only local labels, paths, and short
status messages that already appear elsewhere in xswap's own error text.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time

from codex_swap import SwapError, check_file_store, identity, resolve_openclaw_package_root
from xswap_credentials import CredentialError, read_auth
from xswap_live import LiveError, jwt_claims
from xswap_cli import read_settings

OK, WARN, FAIL = "OK", "WARN", "FAIL"
TOKEN_WARN_SECONDS = 24 * 3600
# Never spawn a probe subprocess with a caller's API key or workload identity in its
# environment; matches the keys Manager.env strips (minus the desktop-only path var).
SECRET_ENV_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")


def check(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


def _finish(label, status, detail, disabled):
    """Per-account checks never fail the run for an account that is out of selection."""
    if disabled:
        if status == FAIL:
            status = WARN
        if status != OK:
            detail = f"{detail} (disabled)"
    return check(label, status, detail)


def _safe_env():
    env = os.environ.copy()
    for key in SECRET_ENV_KEYS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith("CODEX_WORKLOAD_IDENTITY_"):
            env.pop(key)
    return env


def _human_delta(seconds):
    seconds = abs(seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def check_codex_binary():
    executable = shutil.which("codex")
    if not executable:
        return check("codex binary", FAIL, "codex not found on PATH")
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10, env=_safe_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check("codex binary", FAIL, f"{executable}: {exc}")
    output = (result.stdout or result.stderr).strip()
    if result.returncode:
        return check("codex binary", FAIL, f"{executable} --version failed: {output or 'no output'}")
    return check("codex binary", OK, f"{executable} ({output})" if output else executable)


def check_wrapper(settings):
    wrapper = settings.get("wrapper")
    if not wrapper:
        return check("wrapper", OK, "codex command not connected")
    path = Path(wrapper.get("path", ""))
    if path.is_symlink() and os.readlink(path) == wrapper.get("proxy"):
        return check("wrapper", OK, f"{path} -> xswap-codex")
    return check("wrapper", WARN, "codex entry changed outside xswap")


def check_credential_store(manager):
    try:
        check_file_store(manager.source)
    except SwapError as exc:
        return check("credential store", FAIL, str(exc))
    return check("credential store", OK, f"file ({manager.source})")


def check_home(name, home, disabled):
    label = f"{name}: home"
    if not home.exists():
        return _finish(label, FAIL, f"{home} does not exist", disabled)
    if not home.is_dir():
        return _finish(label, FAIL, f"{home} is not a directory", disabled)
    return check(label, OK, str(home))


def check_credentials(name, home, disabled):
    label = f"{name}: credentials"
    try:
        value = identity(home)
    except SwapError as exc:
        return _finish(label, FAIL, str(exc), disabled)
    if value in ("not signed in", "unreadable auth cache"):
        return _finish(label, FAIL, f"{value}; run: xswap login {name}", disabled)
    detail = value + (" (disabled)" if disabled else "")
    return check(label, OK, detail)


def check_token_expiry(name, home, disabled):
    label = f"{name}: token expiry"
    try:
        data = read_auth(home)
    except CredentialError as exc:
        return _finish(label, FAIL, str(exc), disabled)
    if data.get("auth_mode") == "apikey" or data.get("OPENAI_API_KEY"):
        return check(label, OK, "API key credential; no token expiry")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return _finish(label, FAIL, "auth.json has no tokens", disabled)
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token:
        return _finish(label, FAIL, "no access token", disabled)
    claims = jwt_claims(token)
    expiry = claims.get("exp")
    if not isinstance(expiry, (int, float)):
        return check(label, WARN, "access token has no exp claim")
    remaining = expiry - time.time()
    if remaining <= 0:
        return _finish(label, FAIL, f"expired {_human_delta(remaining)} ago; run: xswap login {name}", disabled)
    if remaining < TOKEN_WARN_SECONDS:
        detail = f"expires in {_human_delta(remaining)}; run: xswap login {name}"
        return check(label, WARN, detail + (" (disabled)" if disabled else ""))
    return check(label, OK, f"expires in {_human_delta(remaining)}")


def check_plugins(name, home):
    label = f"{name}: plugins"
    target = home / "plugins"
    if target.is_symlink():
        return check(label, WARN, "run xswap repair-plugins")
    if target.is_dir():
        return check(label, OK, "local cache")
    return check(label, OK, "no plugins")


def check_auto_pool(settings, accounts):
    if not settings.get("enabled"):
        return check("auto pool", OK, "automatic switching not enabled")
    names = settings.get("accounts") or []
    bad = [n for n in names if n not in accounts or accounts[n].get("disabled")]
    if bad:
        return check("auto pool", FAIL, f"pool references unavailable account(s): {', '.join(bad)}")
    return check("auto pool", OK, f"pool: {', '.join(names)}")


def check_auto_runs(manager):
    run_root = manager.root / "auto" / "cli-runs"
    if not run_root.is_dir():
        return check("auto cli-runs", OK, "0 run(s)")
    total = running = 0
    for run_dir in sorted(run_root.iterdir()):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        total += 1
        lock_path = run_dir / ".bridge.lock"
        if not lock_path.exists():
            continue
        fd = os.open(lock_path, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running += 1
        finally:
            os.close(fd)
    return check("auto cli-runs", OK, f"{total} run(s), {running} running")


OPENCLAW_ENTRY_POINTS = ("provider-auth", "agent-runtime", "config-runtime")
_NODE_PROBE = """
const {createRequire} = require('module');
const path = require('path');
const req = createRequire(path.join(process.env.XSWAP_DOCTOR_PACKAGE_ROOT, 'package.json'));
for (const name of process.env.XSWAP_DOCTOR_ENTRIES.split(',')) {
  req.resolve('openclaw/plugin-sdk/' + name);
}
"""


def check_openclaw():
    executable = shutil.which("openclaw")
    node = shutil.which("node")
    if not executable or not node:
        return [check("openclaw", WARN, "OpenClaw sync unavailable: openclaw and node required on PATH")]
    package_root = resolve_openclaw_package_root(executable)
    if package_root is None:
        return [check("openclaw", WARN, "cannot locate OpenClaw's installed package through its executable")]
    env = {**_safe_env(), "XSWAP_DOCTOR_PACKAGE_ROOT": str(package_root), "XSWAP_DOCTOR_ENTRIES": ",".join(OPENCLAW_ENTRY_POINTS)}
    try:
        result = subprocess.run([node, "-e", _NODE_PROBE], capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [check("openclaw", WARN, f"plugin-sdk probe failed: {exc}")]
    if result.returncode:
        output = (result.stderr or result.stdout).strip().splitlines()
        return [check("openclaw", WARN, f"plugin-sdk entry points not resolvable: {output[-1] if output else 'unknown error'}")]
    return [check("openclaw", OK, f"openclaw + node found; plugin-sdk resolvable ({package_root})")]


def check_storage(manager):
    try:
        info = manager.root.lstat()
    except OSError as exc:
        return check("storage", FAIL, str(exc))
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or mode != 0o700:
        return check("storage", FAIL, f"{manager.root} mode={oct(mode)} uid={info.st_uid}")
    return check("storage", OK, str(manager.root))


def run(manager):
    """Return a list of read-only check results. Never mutates state or touches the network."""
    results = [check_codex_binary()]

    try:
        settings = read_settings(manager)
        settings_ok = True
        results.append(check_wrapper(settings))
    except LiveError as exc:
        settings, settings_ok = {}, False
        # One FAIL for the corrupted file; wrapper/auto-pool would only repeat the same cause.
        results.append(check("auto settings", FAIL, str(exc)))

    results.append(check_credential_store(manager))

    try:
        data = manager.read()
        registry_check = check("registry", OK, str(manager.registry))
    except SwapError as exc:
        data = {"accounts": {}}
        registry_check = check("registry", FAIL, str(exc))

    accounts = data.get("accounts", {})
    if not accounts:
        results.append(check("accounts", WARN, "no accounts registered; run: xswap register main"))
    for name, value in accounts.items():
        home = Path(value["home"])
        disabled = bool(value.get("disabled"))
        home_ok = home.is_dir()
        results.append(check_home(name, home, disabled))
        if not home_ok:
            continue
        credentials_check = check_credentials(name, home, disabled)
        results.append(credentials_check)
        if credentials_check["status"] == OK:
            results.append(check_token_expiry(name, home, disabled))
        results.append(check_plugins(name, home))

    if settings_ok:
        results.append(check_auto_pool(settings, accounts))
    results.append(check_auto_runs(manager))
    results.extend(check_openclaw())
    results.append(check_storage(manager))
    results.append(registry_check)
    return results


def format_report(results):
    name_width = max((len(r["name"]) for r in results), default=0)
    lines = [f"{r['status']:<5} {r['name']:<{name_width}}  {r['detail']}".rstrip() for r in results]
    return "\n".join(lines)


def print_report(results, json_output=False):
    if json_output:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(format_report(results))
    return 1 if any(r["status"] == FAIL for r in results) else 0
