"""Reinstall xswap from the latest (or a chosen) released Git tag."""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from typing import Any

from xswap.core.errors import UpgradeError
from xswap.core.path import absolute_which

REPO = "https://github.com/intellieffect/xswap.git"
TAG_RE = re.compile(r"^refs/tags/v(\d+)\.(\d+)\.(\d+)$")

Tag = tuple[tuple[int, int, int], str]


def list_tags(run: Callable[..., Any] | None = None) -> list[Tag]:
    run = run or subprocess.run
    # A relative `git` raises rather than being run: `xswap upgrade` is the command that
    # replaces the installed tool, and a project's own `bin/git` would choose which repository
    # it is replaced from. `or "git"` keeps the not-installed path spelled by the OSError below.
    git = absolute_which("git", error=UpgradeError) or "git"
    try:
        result = run([git, "ls-remote", "--tags", "--refs", REPO], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        raise UpgradeError(f"cannot list release tags: {exc}") from None
    if result.returncode:
        raise UpgradeError("cannot list release tags: git ls-remote failed")
    tags = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        match = TAG_RE.match(parts[1])
        if match:
            tags.append((tuple(int(part) for part in match.groups()), "v" + ".".join(match.groups())))
    if not tags:
        raise UpgradeError("no release tags found")
    return sorted(tags)


def choose(tags: list[Tag], requested: str | None = None) -> Tag:
    if requested:
        for version, tag in tags:
            if tag == requested:
                return version, tag
        raise UpgradeError(f"tag {requested} not found")
    return max(tags)


def install_command(tag: str, uv: str | None = None) -> list[str]:
    return [uv or "uv", "tool", "install", "--force", f"git+{REPO}@{tag}"]


def running_session_hints(target_version: str) -> list[str]:
    """One reopen line per live session that keeps running the previous code after
    a reinstall (`CLI · ai · bridge 0.7.8 · reopen with ... to load 0.8.0`); [] when the
    state cannot be read. Never contains tokens: only account labels, a UUID, paths.
    """
    try:
        from xswap.manager import Manager
        manager = Manager()
        return manager.provider.session_hints(manager, target_version)
    except Exception:
        return []


def upgrade(current_version: str, tag: str | None = None, dry: bool = False) -> int:
    version, chosen = choose(list_tags(), tag)
    if version == tuple(int(part) for part in current_version.split(".")):
        print(f"already up to date (v{current_version})")
        return 0
    command = install_command(chosen)
    if dry:
        print(" ".join(command))
        return 0
    # The installer itself: a relative `uv` is a project file that would be given
    # `tool install --force`, i.e. the contents of the user's whole tool environment.
    uv = absolute_which("uv", error=UpgradeError)
    if uv is None:
        print(" ".join(command))
        return 1
    # Read the session records before the reinstall: afterwards this process still runs
    # the previous code and uv may already have replaced the tool environment under it.
    hints = running_session_hints(chosen[1:])
    result = subprocess.run(install_command(chosen, uv))  # noqa: S603 -- argv list, no shell=True; command/args are program-constructed, not user strings
    if result.returncode == 0 and hints:
        print(f"{len(hints)} running xswap session(s) still use the previous bridge; reopen them to load {chosen}:")
        for line in hints:
            print("  " + line)
    try:
        executable = absolute_which("xswap", error=UpgradeError)
    except UpgradeError as exc:
        # Printing the version of whatever `xswap` this directory holds would confirm an
        # install that did not happen; the reinstall above is already done, so report why
        # the confirmation is missing instead of failing the command.
        print(str(exc))
        executable = None
    if executable:
        check = subprocess.run([executable, "--version"], capture_output=True, text=True)  # noqa: S603 -- argv list, no shell=True; command/args are program-constructed, not user strings
        print((check.stdout or check.stderr).strip())
    return result.returncode
