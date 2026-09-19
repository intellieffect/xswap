"""Directory -> account mappings, stored in the `mappings` key of accounts.json.

Like `Registry`, this holds no lock of its own: `map_dir`/`unmap_dir` run their
read-modify-write inside one `Manager.locked()` acquisition, as before.
"""
from __future__ import annotations

import os
from pathlib import Path

from xswap.errors import SwapError
from xswap.fsutil import atomic_json


class Mappings:
    def __init__(self, manager, hooks):
        self.manager = manager
        self.hooks = hooks

    def best_mapping(self, data, cwd=None):
        target_parts = Path(cwd or os.getcwd()).resolve().parts
        best = None  # (depth, path, name)
        for raw_path, name in data.get("mappings", {}).items():
            parts = Path(raw_path).parts
            if len(parts) <= len(target_parts) and tuple(parts) == target_parts[:len(parts)]:
                if best is None or len(parts) > best[0]:
                    best = (len(parts), raw_path, name)
        return best

    def resolve_default(self, cwd=None):
        """Return (name, mapping_path) for the implicit-selection account, computing the
        directory-mapping lookup exactly once. mapping_path is None when the active
        account (not a mapping) decided the result."""
        data = self.manager.read()
        best = self.manager._best_mapping(data, cwd)
        if best and best[2] in data["accounts"]:
            return best[2], best[1]
        return data["active"], None

    def default_account(self, cwd=None):
        return self.manager.resolve_default(cwd)[0]

    def mapped_source(self, cwd=None):
        """Return the mapping path that decided default_account(cwd), or None."""
        return self.manager.resolve_default(cwd)[1]

    def map_dir(self, name, path=None):
        resolved = str(Path(path or os.getcwd()).expanduser().resolve())
        with self.manager.locked():
            name, _ = self.manager.account(name)
            data = self.manager.read()
            data.setdefault("mappings", {})[resolved] = name
            atomic_json(self.manager.registry, data)
        return resolved, name

    def unmap_dir(self, path=None):
        resolved = str(Path(path or os.getcwd()).expanduser().resolve())
        with self.manager.locked():
            data = self.manager.read()
            mappings = data.get("mappings", {})
            if resolved not in mappings:
                raise SwapError(f"No mapping for {resolved}.")
            del mappings[resolved]
            data["mappings"] = mappings
            atomic_json(self.manager.registry, data)
        return resolved

    def list_mappings(self):
        return dict(self.manager.read().get("mappings", {}))
