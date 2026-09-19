"""Well-known locations under the xswap state root.

Leaf module: stdlib only. Every path here is the one the rest of xswap already
built inline; nothing is renamed or moved on disk. Only locations with more
than one caller get a helper -- `accounts.json`, `usage-cache.json` and
`auth-state.json` have exactly one each and stay on Manager.
"""
from __future__ import annotations

from pathlib import Path

# ALLOW-LIST (INT-5614). This module is the one place in `xswap.core` that may name a
# platform, and only because these two names are on users' disks and in users' shell
# profiles already. Renaming either would move every installed state root and silently
# orphan every registered account, so both stay exactly as they shipped -- they are
# backward-compatible identifiers here, not a dependency on any platform.
#
# The state root every shell reads unless it exports ROOT_VARIABLE: which accounts exist,
# which one is selected, and the record that connects the wrapped command all live under
# it, so the variable silently decides what that plain command does in that shell.
ROOT_VARIABLE = "CODEX_SWAP_HOME"  # allow-listed: the published environment variable


def default_root() -> Path:
    """The state root a shell without ROOT_VARIABLE set uses; a path, never a read of it."""
    return (Path.home() / ".local/share/codex-swap").expanduser().resolve()  # allow-listed: the published state-dir name


def auto_dir(root: str | Path) -> Path:
    """`<root>/auto`: the automatic-switching runtime (bridge state, runtime homes)."""
    return Path(root) / "auto"


def cli_runs_dir(root: str | Path) -> Path:
    """`<root>/auto/cli-runs`: one directory per wrapped CLI run."""
    return auto_dir(root) / "cli-runs"


def settings_path(root: str | Path) -> Path:
    """`<root>/auto.json`: the wrapper/auto-switch record."""
    return Path(root) / "auto.json"


def profile_dir(root: str | Path, name: str) -> Path:
    """`<root>/profiles/<name>`: one account's per-platform homes."""
    return Path(root) / "profiles" / name
