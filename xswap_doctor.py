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

from codex_swap import SwapError, check_file_store, identity
from xswap_credentials import CredentialError, read_auth
from xswap_live import LiveError, jwt_claims
from xswap_cli import read_settings

OK, WARN, FAIL = "OK", "WARN", "FAIL"
TOKEN_WARN_SECONDS = 24 * 3600


def check(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


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
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check("codex binary", FAIL, f"{executable}: {exc}")
    output = (result.stdout or result.stderr).strip()
    if result.returncode:
        return check("codex binary", FAIL, f"{executable} --version failed: {output or 'no output'}")
    return check("codex binary", OK, f"{executable} ({output})" if output else executable)


def check_wrapper(manager):
    try:
        settings = read_settings(manager)
    except LiveError as exc:
        return check("wrapper", WARN, str(exc))
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


def check_home(name, home):
    label = f"{name}: home"
    if not home.exists():
        return check(label, FAIL, f"{home} does not exist")
    if not home.is_dir():
        return check(label, FAIL, f"{home} is not a directory")
    return check(label, OK, str(home))


def check_credentials(name, home, disabled):
    label = f"{name}: credentials"
    try:
        value = identity(home)
    except SwapError as exc:
        return check(label, FAIL, str(exc))
    if value in ("not signed in", "unreadable auth cache"):
        return check(label, FAIL, f"{value}; run: xswap login {name}")
    detail = value + (" (disabled)" if disabled else "")
    return check(label, OK, detail)


def check_token_expiry(name, home):
    label = f"{name}: token expiry"
    try:
        data = read_auth(home)
    except CredentialError as exc:
        return check(label, FAIL, str(exc))
    if data.get("auth_mode") == "apikey" or data.get("OPENAI_API_KEY"):
        return check(label, OK, "API key credential; no token expiry")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return check(label, FAIL, "auth.json has no tokens")
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token:
        return check(label, FAIL, "no access token")
    claims = jwt_claims(token)
    expiry = claims.get("exp")
    if not isinstance(expiry, (int, float)):
        return check(label, WARN, "access token has no exp claim")
    remaining = expiry - time.time()
    if remaining <= 0:
        return check(label, FAIL, f"expired {_human_delta(remaining)} ago; run: xswap login {name}")
    if remaining < TOKEN_WARN_SECONDS:
        return check(label, WARN, f"expires in {_human_delta(remaining)}; run: xswap login {name}")
    return check(label, OK, f"expires in {_human_delta(remaining)}")


def check_plugins(name, home):
    label = f"{name}: plugins"
    target = home / "plugins"
    if target.is_symlink():
        return check(label, WARN, "run xswap repair-plugins")
    if target.is_dir():
        return check(label, OK, "local cache")
    return check(label, OK, "no plugins")


def check_auto_pool(manager, accounts):
    try:
        settings = read_settings(manager)
    except LiveError as exc:
        return check("auto pool", FAIL, str(exc))
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
        return [check("openclaw", WARN, "cannot locate OpenClaw's installed package through its executable")]
    env = {**os.environ, "XSWAP_DOCTOR_PACKAGE_ROOT": str(package_root), "XSWAP_DOCTOR_ENTRIES": ",".join(OPENCLAW_ENTRY_POINTS)}
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
    results = [check_codex_binary(), check_wrapper(manager), check_credential_store(manager)]

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
        home_check = check_home(name, home)
        results.append(home_check)
        if home_check["status"] == FAIL:
            continue
        credentials_check = check_credentials(name, home, disabled)
        results.append(credentials_check)
        if credentials_check["status"] == OK:
            results.append(check_token_expiry(name, home))
        results.append(check_plugins(name, home))

    results.append(check_auto_pool(manager, accounts))
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
