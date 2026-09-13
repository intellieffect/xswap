"""Read-only diagnostics: codex binary, wrapper, credential store, accounts (login, token expiry, rejected logins), auto pool, OpenClaw, storage.

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

from codex_swap import SwapError, __version__, check_file_store, identity, resolve_openclaw_package_root
from xswap_credentials import CredentialError, read_auth
from xswap_live import LiveError, jwt_claims
from xswap_cli import bridge_hints, describe_bridge_hint, describe_drift, entry_drift, link_target_path, read_settings, shadowing_entry, status_data, wrapper_drift, wrapper_state
from xswap_openclaw_state import OpenClawStateError, default_sqlite_path, format_until, profile_id_for_home, read_cooldowns
from xswap_relocate import codex_homes_inside_root, inside_root

OK, WARN, FAIL = "OK", "WARN", "FAIL"
TOKEN_WARN_SECONDS = 24 * 3600
# Everything codex_swap.identity() can return that is not an address: a local label with
# nothing to compare against the login an app server reports.
IDENTITY_SENTINELS = ("not signed in", "unreadable auth cache", "API key", "ChatGPT")
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


def _shown(entry):
    return f"{entry['path']} -> {entry['target']}" if entry.get("target") else entry["path"]


def check_wrapper(settings, accounts=(), env=None):
    """What a PATH lookup of `codex` runs, not only the entry recorded in auto.json.

    2026-09-10: a standalone install at ~/.local/bin/codex preceded the wrapped
    /opt/homebrew/bin/codex on PATH, plain codex bypassed xswap, and this row said OK.
    An entry that bypasses the selection right now is FAIL, which 0.7.8 reported as
    WARN -- the exit code is the only signal a cron or CI check reads. Read-only:
    the xswap_cli helpers only stat and readlink. `env` pins PATH for tests.
    """
    wrapper = settings.get("wrapper") or {}
    names = [name for name, value in dict(accounts).items() if not (value or {}).get("disabled")]
    reconnect = f"xswap auto-enable --accounts {','.join(names) or 'NAME,NAME'} --wrap-codex"
    state, first, entries = wrapper_state(settings, env)
    drift = wrapper_drift(settings)
    if state == "unconfigured":
        status = WARN if len(names) >= 2 else OK
        # xswap deliberately left the entry alone: say which condition and the manual fix, since no
        # later launch, list, or use will close this gap -- promising one (as 0.7.8 did for every
        # non-ok entry) is what kept the 2026-09-10 bypass looking temporary. After `auto-disable`
        # this is the normal state and reads like "not connected".
        cause, fix = describe_drift(drift, reconnect)
        if cause:
            return check("wrapper", status, f"codex command not connected ({drift['reason']}): {cause}. Fix: {fix}")
        detail = "codex command not connected"
        if status == WARN:
            detail += f"; plain codex ignores the xswap selection. Connect it: {reconnect}"
        return check("wrapper", status, detail)
    path = Path(wrapper["path"])
    if state == "absent":
        if drift["reason"] == "ok":
            return check("wrapper", WARN, f"{path} -> xswap-codex, but {path.parent} is not on this shell's PATH; "
                         "plain codex is not found here")
        cause, fix = describe_drift(drift, reconnect)
        return check("wrapper", FAIL, f"no codex on PATH and the wrapped entry {path} no longer runs "
                     f"({drift['reason']}): {cause}. Fix: {fix}")
    shown = _shown(first)
    if state == "connected":
        shadowed = [entry for entry in entries[1:] if entry["kind"] == "foreign"]
        if shadowed:
            return check("wrapper", WARN, f"{first['path']} -> xswap-codex; shadowed on PATH, bypassing xswap "
                         f"only when run by full path: {'; '.join(_shown(entry) for entry in shadowed)}")
        if len(entries) > 1:
            return check("wrapper", OK, f"{first['path']} -> xswap-codex; {len(entries)} codex entries on PATH, all wrapped")
        return check("wrapper", OK, f"{first['path']} -> xswap-codex")
    if state == "drifted":
        if drift["action"] == "reconnect":
            return check("wrapper", FAIL, f"codex entry changed outside xswap (a Codex update replaces the link): {shown}; "
                         "plain codex bypasses the xswap selection until the next xswap launch, list, or use "
                         f"reconnects it. Reconnect it now: {reconnect}")
        cause, fix = describe_drift(drift, reconnect)
        return check("wrapper", FAIL, f"codex entry changed outside xswap ({drift['reason']}): {cause}. Fix: {fix}")
    if shadowing_entry(settings, env) is not None:
        return check("wrapper", FAIL, f"plain codex runs {shown}, not xswap-codex; it shadows the wrapped entry {path} "
                     f"on PATH until the next xswap launch, list, or use wraps it. Wrap it now: {reconnect}")
    shadow = entry_drift(first["path"], wrapper["proxy"])
    if shadow["action"] == "reconnect":
        return check("wrapper", FAIL, f"plain codex runs {shown}, not xswap-codex; the wrapped entry {path} is not "
                     f"on this shell's PATH. Wrap it: {reconnect}")
    cause, fix = describe_drift(shadow, reconnect)
    if not cause:
        # `ok` and `not-wrapped` have no cause sentence; reachable here only when the
        # proxy's realpath could not be read, so the entry was classified foreign.
        return check("wrapper", FAIL, f"plain codex runs {shown}, not xswap-codex; the wrapped entry {path} "
                     f"is not on this shell's PATH. Wrap it: {reconnect}")
    return check("wrapper", FAIL, f"plain codex runs {shown}, not xswap-codex, and xswap cannot wrap it "
                 f"({shadow['reason']}): {cause}. The wrapped entry {path} is shadowed. Fix: {fix}")


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


def check_sign_in(name, home, disabled, manager):
    """The usage service rejected this account's login on a recent quota read
    (auth-state.json) and nothing has cleared it: `xswap login NAME` or a later
    successful live fetch does. Only a record for the current login label counts
    (the usage-cache identity rule). Reads one local file; no process, no network.
    """
    label = f"{name}: sign-in"
    failure = manager.auth_failure(name, identity(home))
    if failure is None:
        return check(label, OK, "no rejected login recorded")
    delta = _human_delta(time.time() - failure["failedAt"])
    return _finish(label, FAIL, f"sign-in required since {delta} ago · xswap login {name}", disabled)


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


def check_auto_dir(manager):
    """The manual-switch control directory must be 0700 or `xswap use` skips running sessions."""
    auto = manager.root / "auto"
    try:
        info = auto.lstat()
    except FileNotFoundError:
        return check("auto control dir", OK, "not created yet")
    except OSError as exc:
        return check("auto control dir", FAIL, str(exc))
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or mode != 0o700:
        return check("auto control dir", WARN, f"{auto} mode={oct(mode)} uid={info.st_uid}; xswap use/switch "
                     f"skips running sessions until: chmod 700 {auto}")
    return check("auto control dir", OK, str(auto))


def check_auto_runs(manager):
    """Count CLI run records and flag live bridges (CLI or desktop) that still run code
    other than the installed xswap, each with the command that reopens it.

    2026-09-10: three 0.7.2 bridges ran next to an installed 0.7.6 and this row said OK.
    Read-only: status_data(cleanup=False) prunes nothing, and the conversation lookup
    only globs the session store. A corrupted auto.json is reported by its own row.
    """
    run_root = manager.root / "auto" / "cli-runs"
    total = running = 0
    if run_root.is_dir():
        for run_dir in sorted(run_root.iterdir()):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            total += 1
            lock_path = run_dir / ".bridge.lock"
            if not lock_path.exists():
                continue
            try:
                fd = os.open(lock_path, os.O_RDONLY)
            except OSError:
                # Sweeps now run from every list/usage, the menu bar's dashboard and every
                # launch, so another one can remove this record between the exists() test
                # and this open. scan_runs is guarded the same way; one vanished record must
                # not abort the whole `xswap doctor` report with a bogus OSError.
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    running += 1
            finally:
                os.close(fd)
    summary = f"{total} run(s), {running} running" if run_root.is_dir() else "0 run(s)"
    try:
        hints = bridge_hints(manager, status_data(manager, cleanup=False), __version__)
    except (LiveError, OSError):
        hints = []
    if not hints:
        return check("auto cli-runs", OK, summary)
    detail = (f"{summary}; {len(hints)} still running an older bridge, reopen to load {__version__}: "
              + "; ".join(describe_bridge_hint(hint) for hint in hints))
    return check("auto cli-runs", WARN, detail)


def check_auto_sessions(manager, accounts):
    """Running bridges: what their own app server confirmed after the last login (0.8.0).

    `account` in a status record is what the bridge believes it installed;
    `verifiedAccount`/`verifiedIdentity` are the app server's answer to account/read.
    2026-09-10: a failed session's record still named the account it had asked for.
    A session on an older bridge has no such answer and is left to the version
    check. A running session whose server never confirmed its login, or whose
    account has since been signed in as someone else, names the command that
    re-sends the account's current login to running bridges. Read-only: status
    records and lock probes only, no process is spawned.
    """
    try:
        sessions = [s for s in status_data(manager, cleanup=False)["sessions"] if s.get("running")]
    except LiveError as exc:
        return check("auto sessions", WARN, f"not checked: {exc}")
    if not sessions:
        return check("auto sessions", OK, "no running sessions")
    older = verified = 0
    problems = []
    for session in sessions:
        surface = session.get("surface") or "unknown"
        pid = session.get("cliPid") or session.get("bridgePid") or "?"
        account = session.get("account")
        if session.get("bridgeVersion") != __version__:
            older += 1
            continue
        if not account or session.get("verifiedAccount") != account:
            reason = session.get("verifyReason") or "not verified"
            problems.append(f"{surface} pid {pid} on {account or 'unknown'}: server login unverified ({reason}); "
                            f"re-send it: xswap switch {account or 'NAME'}")
            continue
        verified += 1
        home = (accounts.get(account) or {}).get("home")
        confirmed = session.get("verifiedIdentity")
        if not home or not isinstance(confirmed, str) or not Path(home).is_dir():
            continue
        try:
            current = identity(Path(home))
        except SwapError:
            continue
        # Every identity() sentinel, not only three of them: "ChatGPT" is what a login with
        # an access token but no email claim reads as locally (check_credentials accepts it
        # as OK), and comparing that label with the server's address claimed a session had
        # changed identity and advised an `xswap switch` that re-verified to the same email.
        if current not in IDENTITY_SENTINELS and current.casefold() != confirmed.casefold():
            problems.append(f"{surface} pid {pid} verified as {confirmed} but {account} is now signed in as {current}; "
                            f"re-send it: xswap switch {account}")
    if problems:
        return check("auto sessions", WARN, "; ".join(problems))
    detail = f"{len(sessions)} running, {verified} verified"
    if older:
        detail += f", {older} on an older bridge (not checked)"
    return check("auto sessions", OK, detail)


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


def _codex_weekly_remaining_from_cache(cache_path, name, label):
    """The codex bucket's weekly-window remainingPercent from usage-cache.json, or
    None if there's no fresh cache entry for this exact identity. Never spawns a
    process or makes a network call -- doctor stays fully offline.
    """
    try:
        data = json.loads(Path(cache_path).read_text())
    except (OSError, ValueError):
        return None
    entry = data.get(name) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or entry.get("identity") != label:
        return None
    buckets = entry.get("buckets")
    if not isinstance(buckets, list):
        return None
    bucket = next((b for b in buckets if isinstance(b, dict) and str(b.get("id") or "").lower() == "codex"), None)
    if not bucket:
        return None
    weekly = None
    for window in bucket.get("windows") or []:
        if not isinstance(window, dict):
            continue
        minutes = window.get("windowMinutes")
        is_weekly = (window.get("position") == "secondary") if minutes is None else (isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes >= 1440)
        if is_weekly and weekly is None:
            weekly = window
    if weekly is None:
        return None
    remaining = weekly.get("remainingPercent")
    return remaining if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) else None


def check_openclaw_cooldown(name, home, disabled, cooldowns, cache_path):
    """One row for an xswap account that has a computable OpenClaw profile id; an
    account with no usable ChatGPT OAuth credential (API key, unreadable, disabled
    with no login) has nothing to check and is omitted entirely (returns None),
    the same way check_token_expiry only runs after credentials already read OK.
    """
    label = f"{name}: openclaw cooldown"
    profile_id = profile_id_for_home(home)
    if profile_id is None:
        return None
    cooldown = cooldowns.get(profile_id)
    if cooldown is None:
        return check(label, OK, "no cooldown")
    until = format_until(cooldown["blockedUntil"])
    reason = cooldown.get("blockedReason") or "unknown"
    weekly = _codex_weekly_remaining_from_cache(cache_path, name, identity(home))
    if weekly is not None and weekly > 0:
        detail = f"blocked {until} ({reason}), but usage-cache shows {weekly:g}% weekly remaining; run: xswap openclaw --clear-cooldown {name}"
        return _finish(label, FAIL, detail, disabled)
    detail = f"blocked {until} ({reason}); run xswap list to verify"
    return _finish(label, WARN, detail, disabled)


def check_openclaw_cooldowns(accounts, cache_path):
    """Skip entirely (no rows) when OpenClaw's state database is absent -- there is
    nothing stale to report yet, matching check_openclaw()'s own WARN-not-FAIL
    treatment of an optional integration that most installs never touch.
    """
    sqlite_path = default_sqlite_path()
    if not sqlite_path.exists():
        return []
    try:
        cooldowns = read_cooldowns(sqlite_path)
    except OpenClawStateError as exc:
        return [check("openclaw cooldowns", WARN, str(exc))]
    rows = []
    for name, value in accounts.items():
        home = Path(value["home"])
        if not home.is_dir():
            continue
        row = check_openclaw_cooldown(name, home, bool(value.get("disabled")), cooldowns, cache_path)
        if row is not None:
            rows.append(row)
    return rows


def check_real_codex(manager, settings, accounts):
    """Where the wrapped codex entry's real executable lives (INT-5186, item 2). Omitted
    when the codex command is not connected. Paths are compared, never moved."""
    wrapper = settings.get("wrapper") or {}
    real = wrapper.get("realCodex")
    if not wrapper or not isinstance(real, str) or not real:
        return None
    names = [name for name, value in dict(accounts).items() if not (value or {}).get("disabled")]
    reconnect = f"xswap auto-enable --accounts {','.join(names) or 'NAME,NAME'} --wrap-codex"
    if not Path(real).is_file():
        return check("real codex", FAIL, f"{real} is missing; plain codex cannot start. Recover: xswap auto-disable, "
                     f"reinstall Codex, then {reconnect}")
    if inside_root(manager, real) or inside_root(manager, wrapper.get("originalTarget")):
        return check("real codex", FAIL, f"{real} is inside the xswap state directory ({manager.root}): a Codex update "
                     "ran inside a bridged session, and purging or resetting auto/ would break plain codex. "
                     "Fix: xswap relocate-codex")
    return check("real codex", OK, real)


def check_packages_links(manager, accounts):
    """One row per xswap-owned Codex home whose `packages` entry exists, plus every auto
    runtime home that exists at all: the entry must link to a home outside xswap's root
    so Codex's updater installs there (see xswap_relocate)."""
    rows = []
    for home in codex_homes_inside_root(manager, accounts):
        packages = home / "packages"
        is_runtime = home.parent == manager.root / "auto"
        if not home.is_dir() or not (is_runtime or packages.is_symlink() or packages.exists()):
            continue
        label = f"packages link: {home.relative_to(manager.root)}"
        if packages.is_symlink():
            target = os.readlink(packages)
            absolute = Path(link_target_path(packages, target))
            # install.sh runs `mkdir -p "$STANDALONE_ROOT"` first, and that fails on a
            # broken link -- an in-session update would die instead of installing.
            if not absolute.exists():
                rows.append(check(label, WARN, f"{packages} -> {target} does not exist: a Codex update from inside "
                                               "a session fails here. Fix: xswap relocate-codex"))
                continue
            try:
                stays_inside = absolute.resolve().is_relative_to(manager.root)
            except (OSError, RuntimeError):
                stays_inside = absolute.is_relative_to(manager.root)
            if stays_inside:
                rows.append(check(label, WARN, f"{packages} -> {target} stays inside {manager.root}; run: xswap relocate-codex"))
            else:
                rows.append(check(label, OK, f"{packages} -> {target}"))
        elif packages.is_dir():
            rows.append(check(label, WARN, f"{packages} is a real directory: a Codex update from inside a session "
                                          "installs here. Fix: xswap relocate-codex"))
        else:
            rows.append(check(label, OK, "not created yet; linked on the next launch"))
    return rows


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

    # Registry first: the wrapper row needs the account count. Output order is unchanged.
    try:
        data = manager.read()
        registry_check = check("registry", OK, str(manager.registry))
    except SwapError as exc:
        data = {"accounts": {}}
        registry_check = check("registry", FAIL, str(exc))
    accounts = data.get("accounts", {})

    try:
        settings = read_settings(manager)
        settings_ok = True
        results.append(check_wrapper(settings, accounts))
    except LiveError as exc:
        settings, settings_ok = {}, False
        # One FAIL for the corrupted file; wrapper/auto-pool would only repeat the same cause.
        results.append(check("auto settings", FAIL, str(exc)))

    results.append(check_credential_store(manager))

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
            results.append(check_sign_in(name, home, disabled, manager))
        results.append(check_plugins(name, home))

    if settings_ok:
        results.append(check_auto_pool(settings, accounts))
        real_codex = check_real_codex(manager, settings, accounts)
        if real_codex is not None:
            results.append(real_codex)
    results.append(check_auto_dir(manager))
    results.extend(check_packages_links(manager, accounts))
    results.append(check_auto_runs(manager))
    results.append(check_auto_sessions(manager, accounts))
    results.extend(check_openclaw())
    results.extend(check_openclaw_cooldowns(accounts, manager.usage_cache_path()))
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
