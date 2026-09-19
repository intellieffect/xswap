"""Codex's read-only diagnostics: the binary, the wrapper, the credential store,
accounts (login, token expiry, rejected logins), the auto pool, OpenClaw, storage.

No mutations, no network, and no secrets in output: only local labels, paths, and short
status messages that already appear elsewhere in xswap's own error text.

The statuses, the result shape and the two renderers are the neutral framework in
`xswap.core.doctor`; everything below decides what to look at. `run` stays here, in
the module the suite patches individual checks on (`patch("xswap.doctor.check_codex_binary")`),
so those patches keep biting: it resolves each check in this module's namespace.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xswap.core.doctor import (  # noqa: F401  re-exported under the names this module has always had
    FAIL,
    OK,
    WARN,
    check,
    format_report,
    print_report,
)
from xswap.core.path import RelativeEntryError, absolute_which
from xswap.core.paths import auto_dir, cli_runs_dir
from xswap.core.settings import read_settings
from xswap.core.types import AccountRecord, CheckPayload
from xswap.manager import (
    ROOT_VARIABLE,
    SwapError,
    __version__,
    check_file_store,
    default_root,
    identity,
    resolve_openclaw_package_root,
)
from xswap.providers.codex.credentials import CredentialError, read_auth
from xswap.providers.codex.live import LiveError, jwt_claims
from xswap.providers.codex.openclaw_state import (
    OpenClawStateError,
    default_sqlite_path,
    format_until,
    profile_id_for_home,
    read_cooldowns,
)
from xswap.providers.codex.relocate import codex_homes_inside_root, inside_root
from xswap.providers.codex.runs import bridge_hints, describe_bridge_hint, status_data
from xswap.providers.codex.wrapper import (
    BYPASS_REASON,
    BYPASS_VARIABLE,
    DRIFT_FIXES,
    RELATIVE_RECORD_REASON,
    bypass_set,
    codex_path_entries,
    describe_drift,
    entry_drift,
    link_target_path,
    path_state,
    recorded_real_codex,
    wrapper_drift,
)

if TYPE_CHECKING:
    from xswap.manager import Manager

TOKEN_WARN_SECONDS = 24 * 3600
# Everything codex_swap.identity() can return that is not an address: a local label with
# nothing to compare against the login an app server reports.
IDENTITY_SENTINELS = ("not signed in", "unreadable auth cache", "API key", "ChatGPT")
# Never spawn a probe subprocess with a caller's API key or workload identity in its
# environment; matches the keys Manager.env strips (minus the desktop-only path var).
SECRET_ENV_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")


def _finish(label: str, status: str, detail: str, disabled: bool) -> CheckPayload:
    """Per-account checks never fail the run for an account that is out of selection."""
    if disabled:
        if status == FAIL:
            status = WARN
        if status != OK:
            detail = f"{detail} (disabled)"
    return check(label, status, detail)


def _safe_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in SECRET_ENV_KEYS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith("CODEX_WORKLOAD_IDENTITY_"):
            env.pop(key)
    return env


def _human_delta(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def check_codex_binary() -> CheckPayload:
    try:
        executable = absolute_which("codex")
    except RelativeEntryError as exc:
        # Running it would execute -- and then report as this machine's Codex -- whatever
        # `codex` the directory doctor was started in holds, which is the one entry xswap
        # refuses to wrap and never re-points. Doctor is read-only, so classify instead:
        # naming the entry is the whole finding, and OK here read as "Codex is fine".
        return check("codex binary", FAIL, str(exc))
    if not executable:
        return check("codex binary", FAIL, "codex not found on PATH")
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10, env=_safe_env())  # noqa: S603 -- argv list, no shell=True; command/args are program-constructed, not user strings
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check("codex binary", FAIL, f"{executable}: {exc}")
    output = (result.stdout or result.stderr).strip()
    if result.returncode:
        return check("codex binary", FAIL, f"{executable} --version failed: {output or 'no output'}")
    return check("codex binary", OK, f"{executable} ({output})" if output else executable)


def _shown(entry: dict[str, Any]) -> str:
    return f"{entry['path']} -> {entry['target']}" if entry.get("target") else entry["path"]


def check_wrapper(
    settings: dict[str, Any],
    accounts: dict[str, AccountRecord] | tuple[()] = (),
    env: dict[str, str] | None = None,
) -> CheckPayload:
    """What a PATH lookup of `codex` runs, not only the entry recorded in auto.json.

    2026-09-10: a standalone install at ~/.local/bin/codex preceded the wrapped
    /opt/homebrew/bin/codex on PATH, plain codex bypassed xswap, and this row said OK.
    An entry that bypasses the selection right now is FAIL, which 0.7.8 reported as
    WARN -- the exit code is the only signal a cron or CI check reads. Read-only:
    the xswap_cli helpers only stat and readlink. `env` pins PATH for tests.

    One PATH walk and one drift classification decide both the verdict and the text.
    The row used to take five independent snapshots, so a concurrent reconnect_wrapper
    (every launch, list and usage read runs one) could pair a verdict from the first
    with a sentence from the last: on a fully connected machine it printed "plain codex
    runs X, not xswap-codex; the wrapped entry Y is not on this shell's PATH" with both
    clauses false, and exited 1 (2026-09-13).
    """
    wrapper = settings.get("wrapper") or {}
    names = [name for name, value in dict(accounts).items() if not (value or {}).get("disabled")]
    reconnect = f"xswap auto-enable --accounts {','.join(names) or 'NAME,NAME'} --wrap-codex"
    # relative=True walks every element, the relative ones included, and the absolute-only
    # list every repair sees is that same walk filtered: one stat of each candidate.
    entries = codex_path_entries(settings, env, relative=True)
    absolute = [entry for entry in entries if Path(entry["path"]).is_absolute()]
    state, first = path_state(settings, absolute)
    drift = wrapper_drift(settings)
    row = _wrapper_row(wrapper, names, reconnect, state, first, absolute, drift)
    # A relative PATH element resolves from the working directory, so xswap never re-points
    # the `codex` it finds there (codex_path_entries leaves it out of every repair), and in
    # this one directory it is what plain `codex` runs. It is a caveat on the rows above,
    # not a verdict of its own: returning it before them reported WARN and exit 0 on a
    # machine permanently bypassed by a foreign regular file, whenever the working directory
    # happened to hold an xswap-codex (2026-09-13). Not said at all while the record is
    # unconfigured or unusable: nothing there claims plain `codex` goes through xswap, and
    # a relative record has no entry to be bypassed.
    ahead = entries[0] if entries and not Path(entries[0]["path"]).is_absolute() else None
    if ahead is None or state == "unconfigured" or drift["reason"] == RELATIVE_RECORD_REASON:
        return row
    status, sentence = _relative_caveat(ahead, Path(wrapper["path"]), alone=row["status"] == OK)
    if row["status"] == OK:
        return check("wrapper", status, sentence)
    return check("wrapper", FAIL if FAIL in (row["status"], status) else row["status"],
                 f"{row['detail']} {sentence}")


def _relative_caveat(ahead: dict[str, Any], path: Path, alone: bool) -> tuple[str, str]:
    """(status, sentence) for the relative PATH element plain `codex` reaches first.

    `alone` when the absolute-PATH rows were OK: the element is then the whole finding and
    the sentence says what plain `codex` runs. Otherwise it is appended to a row that
    already named a condition of its own, and adds only what this directory changes.
    """
    fix = "Fix: make that PATH entry absolute."
    if ahead["kind"] == "wrapper":
        # The same element, resolving to xswap-codex in this directory: plain `codex` here does
        # go through xswap, so FAIL ("not xswap-codex", "keeps using ...") would have been false.
        # It is still the one entry xswap never re-points, and the next directory decides again.
        if alone:
            return WARN, (f"plain codex runs {_shown(ahead)} here, which is xswap-codex, but it is found "
                          "through a relative PATH entry: it names a different file in every directory, which xswap "
                          f"never re-points, so whether plain codex reaches the wrapped entry {path} depends on the "
                          f"directory you run it from. {fix}")
        return WARN, (f"In this directory plain codex runs {_shown(ahead)} first, which is xswap-codex, but it is "
                      "found through a relative PATH entry: it names a different file in every directory, which "
                      f"xswap never re-points, so it hides the condition above here and nowhere else. {fix}")
    if alone:
        return FAIL, (f"plain codex runs {_shown(ahead)} here, not xswap-codex: it is found "
                      "through a relative PATH entry, which names a different file in every directory, so xswap "
                      f"never re-points it and the wrapped entry {path} stays bypassed wherever it resolves. {fix}")
    return FAIL, (f"In this directory plain codex runs {_shown(ahead)} first, not xswap-codex: it is found through "
                  "a relative PATH entry, which names a different file in every directory, so xswap never re-points "
                  f"it and it bypasses the wrapped entry {path} here as well. {fix}")


def _wrapper_row(
    wrapper: dict[str, Any],
    names: list[str],
    reconnect: str,
    state: str,
    first: dict[str, Any] | None,
    entries: list[dict[str, Any]],
    drift: dict[str, Any],
) -> CheckPayload:
    """The wrapper row the absolute PATH entries produce; every branch reads one snapshot."""
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
    # The recorded path itself is not absolute (0.7.8 stored shutil.which's result verbatim), so
    # it names a different file in every working directory: every repair skips it, nothing will
    # close the gap, and the entry xswap really wrapped keeps running xswap-codex with no record
    # left to restore it. Reported before the rows below, which all name wrapper["path"].
    if drift["reason"] == RELATIVE_RECORD_REASON:
        cause, fix = describe_drift(drift, reconnect)
        return check("wrapper", FAIL, f"the wrapper record xswap stored is unusable ({drift['reason']}): {cause}. Fix: {fix}")
    path = Path(wrapper["path"])
    if state == "absent":
        if drift["reason"] == "ok":
            return check("wrapper", WARN, f"{path} -> xswap-codex, but {path.parent} is not on this shell's PATH; "
                         "plain codex is not found here")
        cause, fix = describe_drift(drift, reconnect)
        return check("wrapper", FAIL, f"no codex on PATH and the wrapped entry {path} no longer runs "
                     f"({drift['reason']}): {cause}. Fix: {fix}")
    assert first is not None  # every state left ("connected", "drifted", "shadowed") has a first PATH entry  # noqa: S101 -- narrows an invariant the checker can't see across the call; not user input
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
    # state == 'shadowed'. Both of the next two rows used to ask the filesystem again --
    # shadowing_entry re-walked PATH and re-classified the entry -- so which sentence was
    # printed and what it claimed came from different moments.
    shadow = entry_drift(first["path"], wrapper["proxy"], adopt=True)
    recorded_on_path = any(entry["path"] == wrapper["path"] for entry in entries)
    if shadow["action"] == "reconnect":
        if recorded_on_path:
            return check("wrapper", FAIL, f"plain codex runs {shown}, not xswap-codex; it shadows the wrapped entry {path} "
                         f"on PATH until the next xswap launch, list, or use wraps it. Wrap it now: {reconnect}")
        return check("wrapper", FAIL, f"plain codex runs {shown}, not xswap-codex; the wrapped entry {path} is not "
                     f"on this shell's PATH. Wrap it: {reconnect}")
    cause, fix = describe_drift(shadow, reconnect)
    if not cause:
        # 'ok' has no cause sentence: the entry the walk found foreign reaches xswap-codex by the
        # time it is stat'd. A concurrent reconnect_wrapper did that, or the proxy's realpath could
        # not be read during the walk. Either way the two snapshots disagree, and the old row
        # resolved that by asserting both halves of a sentence it had no evidence for.
        return check("wrapper", WARN, f"plain codex ran {shown} when doctor walked PATH and reaches xswap-codex now "
                     f"({shadow['reason']}): the entry changed while doctor was reading it (any xswap launch, list "
                     "or use reconnects one). Nothing was repaired. Re-run: xswap doctor")
    return check("wrapper", FAIL, f"plain codex runs {shown}, not xswap-codex, and xswap cannot wrap it "
                 f"({shadow['reason']}): {cause}. The wrapped entry {path} is shadowed. Fix: {fix}")
def check_bypass(settings: dict[str, Any], env: dict[str, str] | None = None) -> CheckPayload | None:
    """XSWAP_BYPASS=1 in this environment: `codex` passes through, whatever the wrapper says.

    A separate row because it is true of the shell doctor runs in, not of any entry: the
    variable is documented as a one-command prefix (`XSWAP_BYPASS=1 codex exec resume ...`),
    and once exported it turns the wrapper off for every `codex` in that shell while the
    wrapper row above still reads `-> xswap-codex`. None when it is not set. `env` is the
    environment to judge, os.environ by default; read-only like every other row.
    """
    if not bypass_set(env):
        return None
    wrapper = settings.get("wrapper") or {}
    cause, fix = describe_drift({"action": "skip", "reason": BYPASS_REASON}, "")
    if not (wrapper.get("path") and wrapper.get("proxy") and settings.get("enabled")):
        # Nothing claims plain `codex` goes through xswap, so the variable changes nothing
        # today; it still decides what happens the moment the command is connected.
        return check("codex bypass", WARN, f"{BYPASS_VARIABLE}=1 is set here ({BYPASS_REASON}): {cause}. It takes "
                     f"effect as soon as the codex command is connected. Fix: {fix}")
    return check("codex bypass", FAIL, f"{BYPASS_VARIABLE}=1 is set here ({BYPASS_REASON}): {cause}, so the "
                 f"wrapped entry {wrapper['path']} applies no selection in this shell. Fix: {fix}")


def check_state_root(manager: Manager, env: dict[str, str] | None = None) -> CheckPayload | None:
    """Which auto.json this shell reads, when a variable rather than the default chose it.

    None when CODEX_SWAP_HOME is not set: every shell then reads the same root and the
    storage row already names it. When it is set, the same wrapped `codex` entry answers to
    a different auto.json in any shell, launchd job or desktop app that does not export it
    -- another account and another real Codex, or no record at all, in which case plain
    `codex` reports the real Codex as unavailable and neither repair it prints can run
    there (2026-09-13). Read-only, and the other root is named, never opened.
    """
    if not (os.environ if env is None else env).get(ROOT_VARIABLE):
        return None
    default = default_root()
    if manager.root == default:
        return check("state root", OK, f"{manager.root} ({ROOT_VARIABLE} names the default root)")
    return check("state root", WARN, f"{manager.root}, selected by {ROOT_VARIABLE}; a shell, launchd job or app "
                 f"without that variable reads {default} instead, so plain codex there follows whatever selection "
                 f"that root holds, or none. Fix: export {ROOT_VARIABLE}={manager.root} wherever codex runs")


def check_credential_store(manager: Manager) -> CheckPayload:
    try:
        check_file_store(manager.source)
    except SwapError as exc:
        return check("credential store", FAIL, str(exc))
    return check("credential store", OK, f"file ({manager.source})")


def check_home(name: str, home: Path, disabled: bool) -> CheckPayload:
    label = f"{name}: home"
    if not home.exists():
        return _finish(label, FAIL, f"{home} does not exist", disabled)
    if not home.is_dir():
        return _finish(label, FAIL, f"{home} is not a directory", disabled)
    return check(label, OK, str(home))


def check_credentials(name: str, home: Path, disabled: bool) -> CheckPayload:
    label = f"{name}: credentials"
    try:
        value = identity(home)
    except SwapError as exc:
        return _finish(label, FAIL, str(exc), disabled)
    if value in ("not signed in", "unreadable auth cache"):
        return _finish(label, FAIL, f"{value}; run: xswap login {name}", disabled)
    detail = value + (" (disabled)" if disabled else "")
    return check(label, OK, detail)


def check_token_expiry(name: str, home: Path, disabled: bool) -> CheckPayload:
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


def check_sign_in(name: str, home: Path, disabled: bool, manager: Manager) -> CheckPayload:
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


def check_plugins(name: str, home: Path) -> CheckPayload:
    label = f"{name}: plugins"
    target = home / "plugins"
    if target.is_symlink():
        return check(label, WARN, "run xswap repair-plugins")
    if target.is_dir():
        return check(label, OK, "local cache")
    return check(label, OK, "no plugins")


def check_auto_pool(settings: dict[str, Any], accounts: dict[str, AccountRecord]) -> CheckPayload:
    if not settings.get("enabled"):
        return check("auto pool", OK, "automatic switching not enabled")
    names = settings.get("accounts") or []
    bad = [n for n in names if n not in accounts or accounts[n].get("disabled")]
    if bad:
        return check("auto pool", FAIL, f"pool references unavailable account(s): {', '.join(bad)}")
    return check("auto pool", OK, f"pool: {', '.join(names)}")


def check_auto_dir(manager: Manager) -> CheckPayload:
    """The manual-switch control directory must be 0700 or `xswap use` skips running sessions."""
    auto = auto_dir(manager.root)
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


def check_auto_runs(manager: Manager) -> CheckPayload:
    """Count CLI run records and flag live bridges (CLI or desktop) that still run code
    other than the installed xswap, each with the command that reopens it.

    2026-09-10: three 0.7.2 bridges ran next to an installed 0.7.6 and this row said OK.
    Read-only: status_data(cleanup=False) prunes nothing, and the conversation lookup
    only globs the session store. A corrupted auto.json is reported by its own row.
    """
    run_root = cli_runs_dir(manager.root)
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


def check_auto_sessions(manager: Manager, accounts: dict[str, AccountRecord]) -> CheckPayload:
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


def check_openclaw() -> list[CheckPayload]:
    try:
        executable = absolute_which("openclaw")
        node = absolute_which("node")
    except RelativeEntryError as exc:
        # This row runs both of them, with the package root it then reports. A relative PATH
        # element resolves against the directory doctor was started in, so a repository's own
        # `bin/node` would be the thing probed -- and reported as this machine's OpenClaw
        # install. Same lookup the codex binary row stopped executing; doctor is read-only,
        # so the entry is the finding.
        return [check("openclaw", WARN, str(exc))]
    if not executable or not node:
        return [check("openclaw", WARN, "OpenClaw sync unavailable: openclaw and node required on PATH")]
    package_root = resolve_openclaw_package_root(executable)
    if package_root is None:
        return [check("openclaw", WARN, "cannot locate OpenClaw's installed package through its executable")]
    env = {**_safe_env(), "XSWAP_DOCTOR_PACKAGE_ROOT": str(package_root), "XSWAP_DOCTOR_ENTRIES": ",".join(OPENCLAW_ENTRY_POINTS)}
    try:
        result = subprocess.run([node, "-e", _NODE_PROBE], capture_output=True, text=True, timeout=10, env=env)  # noqa: S603 -- argv list, no shell=True; command/args are program-constructed, not user strings
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [check("openclaw", WARN, f"plugin-sdk probe failed: {exc}")]
    if result.returncode:
        output = (result.stderr or result.stdout).strip().splitlines()
        return [check("openclaw", WARN, f"plugin-sdk entry points not resolvable: {output[-1] if output else 'unknown error'}")]
    return [check("openclaw", OK, f"openclaw + node found; plugin-sdk resolvable ({package_root})")]


def _codex_weekly_remaining_from_cache(cache_path: str | Path, name: str, label: str) -> float | None:
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


def check_openclaw_cooldown(
    name: str, home: Path, disabled: bool, cooldowns: dict[str, dict[str, Any]], cache_path: str | Path
) -> CheckPayload | None:
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


def check_openclaw_cooldowns(accounts: dict[str, AccountRecord], cache_path: str | Path) -> list[CheckPayload]:
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


def check_real_codex(
    manager: Manager, settings: dict[str, Any], accounts: dict[str, AccountRecord]
) -> list[CheckPayload]:
    """Where each wrapped codex entry's real executable lives (INT-5186, item 2). Empty when
    the codex command is not connected. Paths are compared, never moved.

    One row per recorded entry, not only the primary: 0.8.0 keeps the entries a Codex
    install pushed aside in `wrappers`, `auto-disable` restores every one of them, and
    `relocate-codex` rewrites every one of them. Reading only `wrapper` meant the 2026-09-10
    shape -- the standalone installer's entry became the primary with a `realCodex` outside
    the root, the inside-root record moved to `wrappers` -- produced no row at all, so the
    repair relocate was taught to make was never the one doctor asked for.
    """
    wrapper = settings.get("wrapper") or {}
    if not wrapper or not isinstance(wrapper.get("realCodex"), str) or not wrapper["realCodex"]:
        return []
    names = [name for name, value in dict(accounts).items() if not (value or {}).get("disabled")]
    reconnect = f"xswap auto-enable --accounts {','.join(names) or 'NAME,NAME'} --wrap-codex"
    rows = [_real_codex_row(manager, "real codex", wrapper, reconnect, primary=True)]
    for record in settings.get("wrappers") or []:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str) or not record["path"]:
            continue
        if not isinstance(record.get("realCodex"), str) or not record["realCodex"]:
            continue
        rows.append(_real_codex_row(manager, f"real codex: {record['path']}", record, reconnect, primary=False))
    return rows


def _real_codex_row(
    manager: Manager, label: str, record: dict[str, Any], reconnect: str, primary: bool
) -> CheckPayload:
    """One `real codex` row. A secondary record is not what plain `codex` runs today, but
    `auto-disable` puts its path back on PATH pointing at exactly this executable."""
    real = record["realCodex"]
    consequence = ("plain codex cannot start" if primary else
                   f"xswap auto-disable would restore {record['path']} to a Codex that cannot start")
    # A record 0.7.8 wrote can name the release relative to the working directory (it did so
    # whenever the entry it wrapped was relative). This row read it from wherever doctor ran, so
    # it was OK inside a repository that happened to hold that path and FAIL "is missing" -- with
    # a recovery that cannot work -- everywhere else, for the same machine.
    if recorded_real_codex({"wrapper": record}) is None:
        fix = DRIFT_FIXES[RELATIVE_RECORD_REASON].format(reconnect=reconnect)
        return check(label, FAIL, f"{real} is not an absolute path, so it names a different file in every "
                     f"directory; {consequence}. Fix: {fix}")
    if not Path(real).is_file():
        return check(label, FAIL, f"{real} is missing; {consequence}. Recover: xswap auto-disable, "
                     f"reinstall Codex, then {reconnect}")
    if inside_root(manager, real) or inside_root(manager, record.get("originalTarget")):
        broken = ("purging or resetting auto/ would break plain codex" if primary else
                  f"xswap auto-disable would put {record['path']} back on PATH pointing inside it, and purging or "
                  "resetting auto/ would then break plain codex")
        return check(label, FAIL, f"{real} is inside the xswap state directory ({manager.root}): a Codex update "
                     f"ran inside a bridged session, and {broken}. Fix: xswap relocate-codex")
    return check(label, OK, real)


def check_packages_links(manager: Manager, accounts: dict[str, AccountRecord]) -> list[CheckPayload]:
    """One row per xswap-owned Codex home whose `packages` entry exists, plus every auto
    runtime home that exists at all: the entry must link to a home outside xswap's root
    so Codex's updater installs there (see xswap_relocate)."""
    rows = []
    for home in codex_homes_inside_root(manager, accounts):
        packages = home / "packages"
        is_runtime = home.parent == auto_dir(manager.root)
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


def check_storage(manager: Manager) -> CheckPayload:
    try:
        info = manager.root.lstat()
    except OSError as exc:
        return check("storage", FAIL, str(exc))
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or mode != 0o700:
        return check("storage", FAIL, f"{manager.root} mode={oct(mode)} uid={info.st_uid}")
    return check("storage", OK, str(manager.root))


def run(manager: Manager) -> list[CheckPayload]:
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
        bypass = check_bypass(settings)
        if bypass:
            results.append(bypass)
        state_root = check_state_root(manager)
        if state_root:
            results.append(state_root)
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
        results.extend(check_real_codex(manager, settings, accounts))
    results.append(check_auto_dir(manager))
    results.extend(check_packages_links(manager, accounts))
    results.append(check_auto_runs(manager))
    results.append(check_auto_sessions(manager, accounts))
    results.extend(check_openclaw())
    results.extend(check_openclaw_cooldowns(accounts, manager.usage_cache_path()))
    results.append(check_storage(manager))
    results.append(registry_check)
    return results
