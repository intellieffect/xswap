"""Bridge run records: the sweep that prunes them and the `auto-status` report.

Every auto session writes a `status.json` under `auto/cli-runs/<hex>` (the
desktop bridge writes the one in `auto/`). This module is the only reader and
the only remover of those records, and it builds the report `auto-status`,
`doctor`, `upgrade` and the menu bar all read. It sits above `wrapper` -- the
report names what plain `codex` does -- and below `codex_cli`, which sweeps
through `scan_runs` on every launch.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xswap.core.paths import auto_dir, cli_runs_dir
from xswap.core.settings import read_settings
from xswap.providers.codex.live import BRIDGE_LOG_NAME
from xswap.providers.codex.wrapper import (
    BYPASS_REASON,
    RELATIVE_ENTRY_REASON,
    bypass_set,
    wrapper_drift,
    wrapper_state,
)

if TYPE_CHECKING:
    from xswap.manager import Manager


STALE_RUN_SECONDS = 7 * 24 * 3600
STOPPED_RUN_SECONDS = 24 * 3600
MIN_PRUNE_AGE_SECONDS = 60
SESSION_REPORT_LIMIT = 20  # how many records status_data reports; running ones are never dropped
PRUNED_RECORD_KEYS = ('account', 'event', 'bridgeVersion', 'reason')
# The whitelist of bridge status fields a session record may expose. A constant
# because a status.json also holds fields no report may carry (a `stopped`
# record's raw failure payload, anything a newer bridge writes): copying keys by
# name is what keeps `auto-status` free of tokens.
SESSION_KEYS = ('account', 'event', 'switches', 'bridgePid', 'serverPid', 'cliPid', 'updatedAt',
    'bridgeVersion', 'weeklyRemainingThreshold', 'manualSwitchVersion', 'bridgeInstance',
    'manualRequest', 'manualState', 'reason', 'quotaKnown',
    'manualReason', 'candidate', 'lastFailure',
    'conversationId', 'codexHome', 'accounts',
    'verifiedAccount', 'verifiedIdentity', 'verifiedAt', 'verifyReason')


def run_dir_empty(run_dir: Path) -> bool:
    """True when the record holds nothing but (optionally) its lock file.

    Such a record is invisible in every report: the TUI exited before its
    bridge connected, or the bridge died before its first status write. Any
    other file (a status.json, even an unreadable one; a switch.json; a log)
    keeps the record on the ordinary schedule.
    """
    try:
        return all(entry.name == '.bridge.lock' for entry in run_dir.iterdir())
    except OSError:
        return False


def stopped_cleanly(state: dict[str, Any] | None) -> bool:
    """A `stopped` record without a failure reason: the bridge ended the normal way."""
    return isinstance(state, dict) and state.get('event') == 'stopped' and not state.get('reason')


def prune_limit(state: dict[str, Any] | None, empty: bool) -> int:
    """Seconds a non-running record is kept before it is pruned without --prune.

    An empty record has nothing to show. A clean `stopped` record was already
    reported once when the TUI exited; a day keeps it for "what happened
    yesterday". A `stopped` record with a failure reason, or any other last
    event on a record whose lock is free (the bridge ended without its
    `stopped` write: killed, crashed, rebooted), is kept a week so the
    abnormal end stays visible in auto-status.
    """
    if empty:
        return MIN_PRUNE_AGE_SECONDS
    if stopped_cleanly(state):
        return STOPPED_RUN_SECONDS
    return STALE_RUN_SECONDS


def prune_rule(state: dict[str, Any] | None, empty: bool, age: float) -> str:
    """Short label for the schedule a removal fell under; 'forced' only with --prune."""
    if empty:
        return 'empty'
    if age > STALE_RUN_SECONDS:
        return 'stale'
    if stopped_cleanly(state) and age > STOPPED_RUN_SECONDS:
        return 'stopped'
    return 'forced'


def scan_runs(
    manager: Manager, prune: bool = False, cleanup: bool = True
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read every bridge record; with cleanup, remove the ones nobody will read again.

    Returns (sessions, pruned). A record whose lock is held is never removed,
    nor is anything younger than MIN_PRUNE_AGE_SECONDS. Never reads auto.json:
    a launch sweeps through here, and an unreadable settings file must not stop
    a session from starting.
    """
    run_root = cli_runs_dir(manager.root)
    entries = [(auto_dir(manager.root) / 'status.json', None)]
    if run_root.is_dir():
        entries += [(run_dir / 'status.json', run_dir) for run_dir in sorted(run_root.iterdir())
                    if run_dir.is_dir() and not run_dir.is_symlink()]
    sessions = []
    pruned = []
    for path, run_dir in entries:
        state = None
        if path.exists():
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = None
        lock_path = path.parent / '.bridge.lock'
        try:
            fd = os.open(lock_path, os.O_RDONLY) if lock_path.exists() else None
        except OSError:
            # Sweeps now run from list, the menu bar and every launch, so another
            # one can remove this record between the listing and this open.
            continue
        running = False
        try:
            # Holding the fd (and any lock acquired below) through the removal
            # decision keeps a bridge from starting in the same run dir mid-check.
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    running = True
            if (run_dir is not None and not running and run_dir.parent == run_root and
                    run_dir.is_dir() and not run_dir.is_symlink()):
                updated_at = state.get('updatedAt') if isinstance(state, dict) else None
                if isinstance(updated_at, (int, float)):
                    age = time.time() - updated_at
                else:
                    try:
                        age = time.time() - run_dir.stat().st_mtime
                    except OSError:
                        age = 0
                empty = state is None and run_dir_empty(run_dir)
                # A run dir this fresh may still be between creation and the
                # bridge taking its lock (no lock file, no status.json yet);
                # never race that startup window regardless of --prune.
                if cleanup and age > MIN_PRUNE_AGE_SECONDS and (prune or age > prune_limit(state, empty)):
                    try:
                        shutil.rmtree(run_dir)
                    except OSError:
                        # Lost a race with another prune, or the dir vanished;
                        # never let one bad removal crash the whole report.
                        continue
                    fields = state if isinstance(state, dict) else {}
                    pruned.append({'run': run_dir.name, 'rule': prune_rule(state, empty, age),
                                   'ageSeconds': int(age),
                                   **{key: fields.get(key) for key in PRUNED_RECORD_KEYS}})
                    continue
        finally:
            if fd is not None:
                os.close(fd)
        if state is None:
            continue
        log_path = path.parent / BRIDGE_LOG_NAME
        sessions.append({'surface': 'desktop' if run_dir is None else 'cli', 'running': running,
                         'log': str(log_path) if log_path.is_file() and not log_path.is_symlink() else None,
                         **{key: state.get(key) for key in SESSION_KEYS}})
    return sessions, pruned


def reported_sessions(sessions: list[dict[str, Any]], limit: int = SESSION_REPORT_LIMIT) -> list[dict[str, Any]]:
    """Every running record plus the `limit - running` newest stopped ones, in scan order.

    So the list can exceed `limit` when more than `limit` bridges run: the cap bounds
    `auto-status`, but it must not decide what the version check sees. bridge_hints,
    doctor's `auto cli-runs` row, `upgrade` and the menu bar all read this list, and run
    directories are named by uuid4, so a plain tail silently dropped a running bridge once
    the store held more than `limit` readable records -- the desktop record sorts first and
    went first. On 2026-09-10 three 0.7.2 bridges running next to an installed 0.7.6 is
    exactly the state these hints exist to surface.

    `updatedAt` decides which stopped records go, not scan order: uuid4 names carry no
    time, so dropping by position threw away whichever records happened to sort first --
    the desktop record every time, and with it the recent `lastFailure`/`log` a user is
    looking for while keeping a week-old one.
    """
    if len(sessions) <= limit:
        return list(sessions)
    room = max(0, limit - sum(1 for session in sessions if session.get('running')))

    def when(index: int) -> float:
        # Whatever a status.json holds: scan_runs copies `updatedAt` by name without checking
        # it, so a record written by hand or by a newer bridge can carry a string, and mixing
        # those with numbers in one sort would raise out of every report that reads this list.
        value = sessions[index].get('updatedAt')
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    droppable = [index for index, session in enumerate(sessions) if not session.get('running')]
    newest = sorted(droppable, key=when)
    keep = set(newest[-room:] if room else [])
    return [session for index, session in enumerate(sessions)
            if session.get('running') or index in keep]


def status_data(manager: Manager, prune: bool = False, cleanup: bool = True) -> dict[str, Any]:
    settings = read_settings(manager)
    sessions, pruned = scan_runs(manager, prune=prune, cleanup=cleanup)
    drift = wrapper_drift(settings)
    # relative=True: a `codex` a relative PATH element resolves to runs instead of the wrapped
    # entry in this working directory, and reading the absolute-only list reported it as wrapped
    # -- a surface saying the selection is in effect while plain `codex` runs something else,
    # which is the 2026-09-10 incident (doctor already FAILs on it). Reporting only; the repair
    # paths keep codex_path_entries' default and never see that entry.
    state = wrapper_state(settings, relative=True)[0]
    # XSWAP_BYPASS=1 turns the entry point into a pass-through whatever PATH and the record say,
    # so a connected wrapper reported the selection as in effect while plain `codex` ran with the
    # caller's own home. It decides the answer wherever a wrapper is claimed at all.
    bypassed = bypass_set() and state != 'unconfigured'
    return {'enabled': settings.get('enabled', False),
                      'accounts': settings.get('accounts', []),
                      'codexWrapped': state == 'connected' and not bypassed,
                      # The recorded entry's link state says nothing about a PATH element or an
                      # environment variable, so name the condition that decided codexWrapped.
                      # 'auto-disabled' keeps precedence: wrapper_state reports 'unconfigured'
                      # for it, whatever PATH or the environment holds.
                      'wrapperReason': (BYPASS_REASON if bypassed else
                                        RELATIVE_ENTRY_REASON if state == 'relative' else drift['reason']),
                      'weeklyRemainingThreshold': settings.get('weeklyRemainingThreshold', 0),
                      'pruned': len(pruned), 'prunedRuns': pruned, 'sessions': reported_sessions(sessions)}


SURFACE_LABELS = {'cli': 'CLI', 'desktop': 'Desktop'}


def bridge_label(session: dict[str, Any]) -> str:
    """The version a status record reports; every bridge from 0.7.6 on writes it."""
    version = session.get('bridgeVersion')
    return version if isinstance(version, str) and version else 'unknown (older than 0.7.6)'


def reopen_command(session: dict[str, Any], state: dict[str, Any], manager: Manager) -> str | None:
    """Shell-quoted command that reopens this CLI session's conversation on the
    installed code, or None when no saved conversation can be named.

    Only a validated UUID and non-secret settings reach the string. The same
    saved-conversation rule as reconnect_command: a bootstrap thread that never
    took a turn has no rollout file and must not be advertised.
    """
    try:
        thread = str(uuid.UUID(session.get('conversationId')))
    except (ValueError, TypeError, AttributeError):
        return None
    home = session.get('codexHome') or str(auto_dir(manager.root) / 'cli-codex')
    # Through `codex_cli`, which owns `saved_thread` (it belongs with the bridge, not with
    # the run records) and which imports this module at module level, so the import has to
    # stay inside the function; the attribute lookup also keeps
    # `patch('xswap.providers.codex.codex_cli.saved_thread')` biting here.
    from xswap import codex_cli
    if not codex_cli.saved_thread(home, thread):
        return None
    if state.get('codexWrapped'):
        return shlex.join(['codex', 'resume', thread])
    if state.get('enabled'):
        return shlex.join(['xswap', 'run', '--', 'resume', thread])
    names = [n for n in [session.get('account'), *(session.get('accounts') or [])] if isinstance(n, str) and n]
    names = list(dict.fromkeys(names))
    if len(names) < 2:
        return None
    return shlex.join(['xswap', 'run', '--auto', '--accounts', ','.join(names), '--', 'resume', thread])


def bridge_hint(session: dict[str, Any], state: dict[str, Any], manager: Manager, target_version: str) -> str:
    """One English line saying how to bring a running session onto target_version."""
    label = bridge_label(session)
    if session.get('surface') == 'desktop':
        return f'bridge {label} · quit and reopen it with xswap app to load {target_version}'
    command = reopen_command(session, state, manager)
    if command:
        return f'bridge {label} · reopen with {command} to load {target_version}'
    return f'bridge {label} · exit and reopen it to load {target_version}'


def bridge_hints(manager: Manager, state: dict[str, Any], target_version: str) -> list[dict[str, Any]]:
    """Running sessions whose bridge is not target_version, each with its reopen hint.

    `state` is a status_data() result. A stopped record is never listed: only a live
    bridge keeps running old code. A record without a version predates 0.7.6 and
    counts as outdated. Plain string inequality, so a downgrade (`upgrade --tag` to an
    older release) is flagged too and the text stays true.
    """
    hints = []
    for session in state.get('sessions') or []:
        if not session.get('running') or session.get('bridgeVersion') == target_version:
            continue
        hints.append({'surface': session.get('surface'), 'account': session.get('account'),
                      'bridgeVersion': session.get('bridgeVersion'),
                      'hint': bridge_hint(session, state, manager, target_version)})
    return hints


def describe_bridge_hint(hint: dict[str, Any]) -> str:
    """`CLI · ai · bridge 0.7.2 · reopen with ... to load 0.8.0` for doctor and upgrade."""
    return f"{SURFACE_LABELS.get(hint['surface'], 'Unknown')} · {hint['account'] or 'unknown'} · {hint['hint']}"


def show_status(manager: Manager, prune: bool = False) -> None:
    # Looked up through `codex_cli` at call time, like `display` does: the suite
    # patches `xswap.providers.codex.codex_cli.status_data`, and `show_status` used to read that
    # global. Drop once the re-export goes away.
    from xswap import codex_cli
    from xswap.core.json_output import auto_status_payload
    print(json.dumps(auto_status_payload(codex_cli.status_data(manager, prune=prune)), indent=2))
