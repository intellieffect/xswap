"""One PATH lookup for every executable xswap runs or names.

shutil.which joins the raw PATH element, so a relative one -- a project's `bin`, a
direnv habit, or the empty element POSIX reads as the working directory -- comes back
as a relative path: the executable of whatever directory xswap happened to run in.
Every caller here either execs that answer inside a pool account's home or bakes it
into something long-lived (a launchd job, a desktop app's recorded interpreter path),
where the working directory of one shell would decide which file runs, for ever. The
one lookup that does not go through here is a provider's own binary lookup: it has a
recorded release to fall back to, so a relative hit is a reason to run the record
instead of refusing.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from xswap.core.errors import XswapError


class RelativeEntryError(XswapError):
    """`name` is on PATH only through a relative element."""


def relative_entry_message(name: str, found: str) -> str:
    """The sentence every surface uses for a relative PATH hit, verbatim."""
    return (f"{name} was found through a relative PATH entry ({found}), which names a different "
            "file in every directory, so xswap will not run it. Fix: make that PATH entry absolute, "
            "then retry.")


def absolute_which(name: str, error: type[Exception] = RelativeEntryError) -> str | None:
    """shutil.which(name) when it is absolute; None when nothing was found at all.

    A relative answer raises `error` -- the caller's own error class, so the message
    reaches the user the way that surface reports every other failure -- instead of
    being returned. `None` still means "not installed", so each caller keeps its own
    wording for that. A surface that must not raise (doctor is read-only and has to
    keep reporting) catches RelativeEntryError and reports the entry rather than
    executing it.
    """
    found = shutil.which(name)
    if found is None or Path(found).is_absolute():
        return found
    raise error(relative_entry_message(name, found))
