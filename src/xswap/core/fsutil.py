"""Filesystem primitives shared across xswap.

Leaf module: stdlib only, so every other module can use it without an import
cycle. Only what is provably identical for every caller lives here --
`credentials.read_auth`, `switch.read_private_json` and the bridge log writers
in `live` each verify a file their own way (different accepted modes, a size
cap in one, different exception types and messages), so they stay where they
are rather than being unified into one helper that would have to loosen one of
them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def private_dir(path: Path, error):
    """Create/repair `path` as a user-owned 0700 directory, or raise `error`.

    The error class is the caller's (Manager passes SwapError) so the message
    reaches the user the way that surface reports every other failure -- the
    same convention `path.absolute_which` uses.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise error(f"Unsafe storage directory: {path}")
    path.chmod(0o700)


def atomic_json(path: Path, data):
    fd, tmp = tempfile.mkstemp(prefix=".swap-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
