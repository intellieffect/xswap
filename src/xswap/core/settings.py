"""The one reader of `auto.json`, the automatic-switching settings file.

A leaf by design: every surface that decides anything from these settings
(the wrapper, the launcher, `doctor`, `tick`, the provider modules, the
`Manager` collaborators) reads them through here, and this module imports
nothing from `xswap` but one error class and `paths`. There is no
matching writer function -- each writer owns the read-modify-write under the
root lock and commits with `atomic_json(settings_path(manager.root), ...)`
itself, which is what keeps a concurrent `reconnect_wrapper` from being
discarded (see `wrapper.enable`).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xswap.core.errors import LiveError
from xswap.core.paths import settings_path

if TYPE_CHECKING:
    from xswap.manager import Manager


def read_settings(manager: Manager) -> dict[str, Any]:
    path = settings_path(manager.root)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise LiveError('invalid auto-mode settings; refusing to overwrite') from None
