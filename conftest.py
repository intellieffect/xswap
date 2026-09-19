"""Process-global guard: the test suite cannot write the developer's REAL stores.

Every test in this repo isolates itself with `TemporaryDirectory` and
`patch.dict(os.environ, ...)`. That is isolation by convention: it holds only
while the patch is in scope, and only for code that reads the environment
*after* the patch went in. Two shapes escape it -- a test that forgets a
patch, and a thread a test started that runs its write after teardown has
already unwound the patch (this suite spawns them: the auto-switch loop, the
alert tick, the bridge server). Both then resolve `codex_swap.default_root()`
to the developer's live `~/.local/share/codex-swap` and overwrite the real
`accounts.json`.

The guard here is a `sys.addaudithook`, installed once at import. CPython
offers no removal API by design, so unlike a fixture it cannot unwind: a
thread outliving its test is still refused.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


class RealStoreWriteBlocked(Exception):
    """A test tried to write a REAL account store instead of a tmp one.

    Deliberately NOT an `OSError` subclass. `Path.mkdir(parents=True,
    exist_ok=True)` catches `OSError` internally and swallows it when the
    target already exists -- and every protected root here *does* exist on a
    developer machine -- so an `OSError`-based refusal would fire and then
    never reach the caller.
    """


def _freeze_real_roots() -> tuple[Path, ...]:
    """Snapshot the REAL roots ONCE, at import, before any fixture moves HOME.

    Re-resolving at check time would be tautological: during an isolated test
    `default_root()` correctly points at that test's own tmp directory, so
    "does this write land where xswap resolves right now" is true for every
    legitimate write too. A frozen reference makes the exemption fall out for
    free -- a tmp path simply never equals a frozen real one -- and it is
    exactly when a test's isolation has unwound that the two match again.

    Both the literal and the symlink-resolved spelling of each root are kept:
    on this machine `~/Projects` and friends are symlinks onto an external
    volume, so `/Users/x/.codex` and `/Volumes/.../.codex` are the same store
    under two names and a check against only one of them is a hole.
    """
    home = Path(os.path.expanduser("~"))
    candidates = [
        home / ".local" / "share" / "codex-swap",  # codex_swap.default_root()
        home / ".codex",  # the real Codex home accounts can register
        home / ".openclaw",  # OpenClaw agent state
    ]
    # A developer with either variable exported in their normal shell has
    # their real store at the override, not at the default above.
    for variable in ("CODEX_SWAP_HOME", "CODEX_HOME"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value).expanduser())

    roots: list[Path] = []
    for candidate in candidates:
        for spelling in (candidate, _resolved(candidate)):
            spelling = Path(os.path.normpath(str(spelling)))
            if spelling != home and spelling not in roots:
                roots.append(spelling)
    return tuple(roots)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


_REAL_ROOTS = _freeze_real_roots()

# Cheap pre-filter, checked as a plain substring before any `Path` is built:
# the hook runs on EVERY `open` in the process (imports, pytest internals,
# stdlib chatter), and the overwhelming majority carry none of these.
_HINTS = tuple({root.name for root in _REAL_ROOTS if root.name})

# Audit events that announce a write. Names and argument shapes are CPython's
# (`sys.audit` table): `shutil.rmtree`/`shutil.move` have no events of their
# own beyond `shutil.rmtree`, and `os.replace`/`os.link` ride on `os.rename`
# and `os.link` respectively. `os.chmod` is included because a mode change on
# the real store is still a mutation of it.
_WRITE_EVENTS = frozenset({
    "open",
    "os.rename",
    "os.mkdir",
    "os.remove",
    "os.rmdir",
    "os.symlink",
    "os.link",
    "os.chmod",
    "os.truncate",
    "shutil.rmtree",
})


def _is_write_open(mode, flags) -> bool:
    """Whether an `open` audit event is a write rather than a plain read.

    `io.open`/`pathlib` deliver `mode` as a string; `os.open` delivers
    `mode=None` and only `flags`, so both shapes must be read. The access
    mask is derived from `O_WRONLY|O_RDWR` rather than `os.O_ACCMODE`, which
    is POSIX-only and would make the hook raise on Windows.
    """
    if mode is not None:
        return any(character in mode for character in ("w", "a", "x", "+"))
    if not isinstance(flags, int):
        return False
    if (flags & (os.O_WRONLY | os.O_RDWR)) != os.O_RDONLY:
        return True
    return bool(flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND))


def _candidates(event: str, args: tuple) -> tuple:
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int) or not _is_write_open(mode, flags):
            return ()  # an already-open fd, or a read
        return (path,)
    if event == "os.rename":
        # `os.replace` and `shutil.move`'s fast path fire this too. The
        # SOURCE counts as well as the destination: moving the store out of
        # the place every reader expects destroys it just as thoroughly.
        return (args[0], args[1])
    if event in ("os.symlink", "os.link"):
        # args are (src, dst, ...): `src` is the existing target and is not
        # written; `dst` is the name being created, which is the write.
        return (args[1],)
    if event == "shutil.rmtree":
        # Fires once at the top, before a child is touched. The fd-relative
        # walk underneath removes children by name against an open directory
        # fd, which no per-child event can be matched to an absolute path --
        # so this is the only place the recursive delete is visible.
        # `sys.audit` passes the caller's argument verbatim, so this one may
        # be a `Path` where every other event delivers `str`.
        try:
            return (os.fspath(args[0]),)
        except TypeError:
            return ()
    return (args[0],)  # os.mkdir / os.remove / os.rmdir / os.chmod / os.truncate


def _real_store_audit_hook(event: str, args: tuple) -> None:
    if event not in _WRITE_EVENTS:
        return
    try:
        candidates = _candidates(event, args)
    except (IndexError, ValueError):  # an argument shape this CPython spells differently
        return
    for candidate in candidates:
        if candidate is None or isinstance(candidate, int):
            continue
        if isinstance(candidate, bytes):
            # A bytes path would otherwise be substring-tested against a str
            # hint tuple, which is never true -- a silent skip, not a pass.
            try:
                candidate = os.fsdecode(candidate)
            except (UnicodeDecodeError, ValueError):
                candidate = str(candidate)
        elif not isinstance(candidate, str):
            try:
                candidate = os.fspath(candidate)
            except TypeError:
                continue
        if not os.path.isabs(candidate):
            # A relative path is resolved against the cwd by every syscall
            # guarded here, so join before comparing -- unconditionally, since
            # a relative spelling that already carries a hint still can never
            # equal an absolute root.
            candidate = os.path.join(os.getcwd(), candidate)
        if not any(hint in candidate for hint in _HINTS):
            continue  # cheap reject: the common case
        target = Path(os.path.normpath(candidate))
        for root in _REAL_ROOTS:
            if target == root or root in target.parents:
                raise RealStoreWriteBlocked(
                    f"{event} refused: {target} is inside the REAL store at "
                    f"{root}, not an isolated tmp directory. Fix the test's "
                    "isolation, not this guard."
                )


sys.addaudithook(_real_store_audit_hook)


@pytest.fixture(autouse=True)
def _isolate_real_state(tmp_path_factory, monkeypatch):
    """Point the state variables at a throwaway directory for every test.

    A belt to the audit hook's braces: the hook refuses a real write, this
    keeps a test that forgot its own `patch.dict` from attempting one at all.
    Tests that set these themselves win -- `patch.dict` is entered after this
    fixture and restores to the value seen here, which is the tmp one.

    `HOME` is deliberately NOT redirected. `xswap upgrade`, the launchd
    installer and the OpenClaw sync shell out to `git`, `uv`, `security` and
    `node`, all of which read `$HOME` for their own configuration, and the
    macOS Keychain lookups resolve `~/Library/Keychains` -- an isolated HOME
    makes those fail for reasons that have nothing to do with what is being
    tested. The two variables below are what `codex_swap` itself resolves
    from, and the frozen audit hook covers the `$HOME`-derived defaults.
    """
    root = tmp_path_factory.mktemp("xswap-state")
    monkeypatch.setenv("CODEX_SWAP_HOME", str(root / "state"))
    monkeypatch.setenv("CODEX_HOME", str(root / "codex-home"))
