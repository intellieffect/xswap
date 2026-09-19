"""One flock per lock file, as context managers.

Leaf module: stdlib only. `Manager.locked` / `Manager.try_locked`
are thin wrappers around these; the semantics are theirs, unchanged: the lock
file is created 0600 on first use, `locked` waits, `try_locked` yields False
instead of waiting, and both always close the descriptor (which releases the
lock) on the way out.
"""
from __future__ import annotations

import contextlib
import fcntl
import os

LOCK_FILE_MODE = 0o600


@contextlib.contextmanager
def locked(path):
    """Hold an exclusive lock on `path`, waiting for whoever has it."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, LOCK_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def try_locked(path):
    """locked(), but yields False instead of waiting when someone else holds the lock."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, LOCK_FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)
