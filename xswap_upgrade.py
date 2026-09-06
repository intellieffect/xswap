"""Reinstall xswap from the latest (or a chosen) released Git tag."""
from __future__ import annotations

import re
import shutil
import subprocess

REPO = "https://github.com/intellieffect/xswap.git"
TAG_RE = re.compile(r"^refs/tags/v(\d+)\.(\d+)\.(\d+)$")


class UpgradeError(Exception):
    pass


def list_tags(run=None):
    run = run or subprocess.run
    try:
        result = run(["git", "ls-remote", "--tags", "--refs", REPO], capture_output=True, text=True, timeout=30)
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


def choose(tags, requested=None):
    if requested:
        for version, tag in tags:
            if tag == requested:
                return version, tag
        raise UpgradeError(f"tag {requested} not found")
    return max(tags)


def install_command(tag):
    return ["uv", "tool", "install", "--force", f"git+{REPO}@{tag}"]


def upgrade(current_version, tag=None, dry=False):
    version, chosen = choose(list_tags(), tag)
    if version == tuple(int(part) for part in current_version.split(".")):
        print(f"already up to date (v{current_version})")
        return 0
    command = install_command(chosen)
    if dry:
        print(" ".join(command))
        return 0
    if shutil.which("uv") is None:
        print(" ".join(command))
        return 1
    result = subprocess.run(command)
    executable = shutil.which("xswap")
    if executable:
        check = subprocess.run([executable, "--version"], capture_output=True, text=True)
        print((check.stdout or check.stderr).strip())
    return result.returncode
