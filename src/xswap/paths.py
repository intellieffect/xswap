"""Well-known locations under the xswap state root.

Leaf module: stdlib only. Every path here is the one the rest of xswap already
built inline; nothing is renamed or moved on disk. Only locations with more
than one caller get a helper -- `accounts.json`, `usage-cache.json` and
`auth-state.json` have exactly one each and stay on Manager.
"""
from __future__ import annotations

from pathlib import Path

# The state root every shell reads unless it exports ROOT_VARIABLE: which accounts exist,
# which one is selected, and the record that connects the `codex` command all live under it,
# so the variable silently decides what plain `codex` does in that shell (2026-09-13).
ROOT_VARIABLE = "CODEX_SWAP_HOME"


def default_root():
    """The state root a shell without CODEX_SWAP_HOME uses; a path, never a read of it."""
    return (Path.home() / ".local/share/codex-swap").expanduser().resolve()


def auto_dir(root):
    """`<root>/auto`: the automatic-switching runtime (bridge state, runtime homes)."""
    return Path(root) / "auto"


def cli_runs_dir(root):
    """`<root>/auto/cli-runs`: one directory per `xswap-codex` run."""
    return auto_dir(root) / "cli-runs"


def settings_path(root):
    """`<root>/auto.json`: the wrapper/auto-switch record."""
    return Path(root) / "auto.json"


def profile_dir(root, name):
    """`<root>/profiles/<name>`: one account's Codex and desktop homes."""
    return Path(root) / "profiles" / name
