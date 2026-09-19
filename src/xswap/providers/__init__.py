"""The provider registry: one entry per platform xswap can switch accounts for.

`get(name)` is the only way core-adjacent code reaches a platform. Nothing is
imported at module level -- a provider module imports `xswap.core` and is
imported by `xswap.manager`, so an eager import here would close that loop.
"""
from __future__ import annotations

DEFAULT = "codex"

# name -> "module:attribute" of the Provider implementation, resolved on first use.
_PROVIDERS = {"codex": "xswap.providers.codex.provider:CodexProvider"}

_cache = {}


def names():
    """Every provider name this build knows, in registration order."""
    return tuple(_PROVIDERS)


def get(name=None):
    """The Provider for NAME (default: `DEFAULT`), constructed once per process."""
    name = name or DEFAULT
    if name in _cache:
        return _cache[name]
    try:
        path = _PROVIDERS[name]
    except KeyError:
        raise LookupError(f"unknown provider {name!r}; known providers: {', '.join(_PROVIDERS)}") from None
    import importlib
    module_name, _, attribute = path.partition(":")
    provider = getattr(importlib.import_module(module_name), attribute)()
    _cache[name] = provider
    return provider
