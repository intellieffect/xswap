"""Documented process exit codes shared by `auto-tick` and other CLI commands.

These numbers are a contract with launchd/cron wrappers and scripts that grep xswap's
exit status (see docs/exit-codes.md); do not renumber an existing member.
"""
from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3
