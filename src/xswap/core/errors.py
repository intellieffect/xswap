"""Every exception xswap raises, rooted in one class.

Leaf module: it imports nothing from `xswap`, so any module -- including the
other leaf modules -- can raise from here without creating an import cycle.

`XswapError` is the common parent so an embedder can catch "xswap failed" in
one clause. The subclasses keep their original names, messages and existing
relations (`BusyThreadError` is still a `LiveError`): each one is still
imported from, and re-raised by, the module that owned it, and each module
re-exports its own so `from xswap.<module> import <Error>` keeps working.
"""
from __future__ import annotations


class XswapError(Exception):
    """Base class for every error xswap raises on purpose."""


class SwapError(XswapError):
    pass


class AlertError(XswapError):
    pass


class CredentialError(XswapError):
    pass


class OpenClawStateError(XswapError):
    pass


class UsageError(XswapError):
    pass


class UpgradeError(XswapError):
    pass


class LiveError(XswapError):
    pass


class BusyThreadError(LiveError):
    pass
