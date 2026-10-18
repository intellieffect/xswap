"""Supply definitions only; hook trust and account state are never shared here."""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from xswap.core.types import AccountRecord

if TYPE_CHECKING:
    from xswap.manager import Manager


def supply_definitions(source: Path, home: Path) -> None:
    """Link missing root profiles/hooks, preserving even dangling destination links."""
    source = source.absolute()
    for src in [*sorted(source.glob("*.config.toml")), source / "hooks.json"]:
        dst = home / src.name
        if src.is_file() and not dst.exists() and not dst.is_symlink():
            with contextlib.suppress(FileExistsError):
                dst.symlink_to(src)


def refresh_managed(manager: Manager, home: Path,
                    accounts: dict[str, AccountRecord] | None = None) -> None:
    """Only explicit Codex-managed homes inside the managed tree may be refreshed."""
    if manager.provider.name != "codex":
        return
    accounts = manager.read()["accounts"] if accounts is None else accounts
    root = manager.root / "profiles"
    if not home.absolute().is_relative_to(root.absolute()) or not home.resolve().is_relative_to(manager.root.resolve() / "profiles"):
        return
    if any(entry.get("managed") and entry.get("provider", "codex") == "codex"
           and Path(entry["home"]).absolute() == home.absolute()
           for entry in accounts.values()):
        supply_definitions(manager.source, home)


def supply_runtime(manager: Manager, name: str, home: Path) -> Path:
    """Refresh managed accounts first, then inherit the selected home's overrides."""
    _, source = manager.account(name)
    refresh_managed(manager, source)
    for entry in ("config.toml", "AGENTS.md", "skills", "rules"):
        src, dst = source / entry, home / entry
        if src.exists() and not dst.exists() and not dst.is_symlink():
            # Concurrent launches may win the same missing-only link race.
            with contextlib.suppress(FileExistsError):
                dst.symlink_to(src, target_is_directory=src.is_dir())
    supply_definitions(source, home)
    return source
