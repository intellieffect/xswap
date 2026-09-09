"""Private, token-free requests to already running xswap bridges."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import time
import uuid


def read_private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or info.st_size > 65536):
            raise ValueError('unsafe control file')
        with os.fdopen(fd, closefd=False) as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError('invalid control file')
        return value
    finally:
        os.close(fd)


def private_directory(path):
    info = path.lstat()
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
            stat.S_IMODE(info.st_mode) == 0o700)


def switch_running(manager, name, timeout=2, only_current=False):
    """Broadcast once; a request is bound to one live bridge instance.

    No source credentials are copied. Acknowledgements distinguish acceptance
    from completed login, and old processes are never signalled or restarted.
    With only_current, bridges whose current account is not NAME are left
    alone (a re-login re-authenticates the sessions already on that account;
    the others re-read the home when they next consider it).
    """
    from codex_swap import atomic_json
    report = dict(applied=0, pending=0, unsupported=0, failed=0, unconfirmed=0)
    auto = manager.root / 'auto'
    if not auto.exists():
        return report
    if not private_directory(auto):
        # Nothing is signalled through a directory other users could write to; say so.
        report['unsafe'] = str(auto)
        return report
    paths = [auto]
    runs = auto / 'cli-runs'
    if runs.exists() and private_directory(runs):
        paths.extend(sorted(runs.iterdir()))
    waiting = []
    for directory in paths:
        try:
            if not private_directory(directory):
                continue
            fd = os.open(directory / '.bridge.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    continue  # No live owner; stale records must not receive requests.
                except BlockingIOError:
                    pass
                state = read_private_json(directory / 'status.json')
                instance = state.get('bridgeInstance')
                if state.get('manualSwitchVersion') != 1 or not isinstance(instance, str):
                    report['unsupported'] += 1
                    continue
                if only_current and state.get('account') != name:
                    continue
                request_id = uuid.uuid4().hex
                atomic_json(directory / 'switch.json', {
                    'id': request_id, 'instance': instance, 'account': name})
                waiting.append((directory, instance, request_id))
            finally:
                os.close(fd)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            report['failed'] += 1
    deadline = time.monotonic() + timeout
    while waiting:
        remaining = []
        for directory, instance, request_id in waiting:
            try:
                state = read_private_json(directory / 'status.json')
            except (OSError, ValueError):
                state = {}
            acknowledged = (state.get('bridgeInstance') == instance and
                            state.get('manualRequest') == request_id)
            outcome = state.get('manualState') if acknowledged else None
            if outcome in ('applied', 'failed'):
                report[outcome] += 1
            elif time.monotonic() >= deadline:
                report['pending' if outcome in ('pending', 'applying') else 'unconfirmed'] += 1
            else:
                remaining.append((directory, instance, request_id))
        waiting = remaining
        if waiting:
            time.sleep(0.05)
    return report
