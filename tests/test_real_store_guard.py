"""The guard in conftest.py refuses writes to the developer's REAL stores.

Every case uses a unique filename and asserts afterwards that nothing was
created: the audit hook fires BEFORE the syscall, so a refusal that is real
leaves the protected root byte-identical.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path

import pytest
from conftest import _REAL_ROOTS, RealStoreWriteBlocked


def _probe(root: Path) -> Path:
    return root / f"xswap-guard-probe-{uuid.uuid4().hex}.json"


@pytest.mark.parametrize("root", _REAL_ROOTS, ids=lambda r: str(r))
def test_open_for_write_is_refused(root):
    target = _probe(root)
    with pytest.raises(RealStoreWriteBlocked):
        with open(target, "w") as handle:
            handle.write("x")
    assert not target.exists()


@pytest.mark.parametrize("root", _REAL_ROOTS, ids=lambda r: str(r))
def test_mkdir_is_refused(root):
    target = _probe(root)
    with pytest.raises(RealStoreWriteBlocked):
        os.mkdir(target)
    assert not target.exists()


@pytest.mark.parametrize("root", _REAL_ROOTS, ids=lambda r: str(r))
def test_replace_into_the_root_is_refused(root, tmp_path):
    source = tmp_path / "staged.json"
    source.write_text("{}")
    target = _probe(root)
    with pytest.raises(RealStoreWriteBlocked):
        os.replace(source, target)
    assert not target.exists()
    assert source.exists()  # the atomic rename never happened


@pytest.mark.parametrize("root", _REAL_ROOTS, ids=lambda r: str(r))
def test_path_mkdir_exist_ok_into_an_existing_root_is_refused(root):
    """`Path.mkdir(parents=True, exist_ok=True)` on a root that ALREADY exists.

    This is the case that decides the exception's base class: pathlib catches
    `OSError` here and swallows it when the directory is present, so an
    `OSError`-derived refusal would be invisible to the caller.
    """
    if not root.exists():
        pytest.skip(f"{root} does not exist on this machine")
    with pytest.raises(RealStoreWriteBlocked):
        root.mkdir(parents=True, exist_ok=True)


def test_a_thread_outliving_the_env_patch_is_still_refused(monkeypatch):
    """The shape a fixture-based guard cannot catch.

    The thread is started inside the test but does its write only after the
    environment patches have been undone -- `monkeypatch.undo()` stands in
    for the teardown that a real late-running thread would race. It then
    resolves the state root from the process-global environment, gets the
    developer's real one back, and writes there. The audit hook has no
    removal API, so it is still armed when a fixture-based guard would be
    long gone.
    """
    root = _REAL_ROOTS[0]
    target = _probe(root)
    monkeypatch.setenv("CODEX_SWAP_HOME", str(root / "unused"))
    start = threading.Event()
    failure: list[BaseException] = []

    def write_late():
        start.wait(timeout=5)
        try:
            resolved = Path(os.environ.get("CODEX_SWAP_HOME") or root)
            resolved.mkdir(parents=True, exist_ok=True)
            target.write_text("{}")
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            failure.append(exc)

    worker = threading.Thread(target=write_late)
    worker.start()
    monkeypatch.undo()  # teardown, early and explicit: the patch is gone
    start.set()
    worker.join(timeout=5)

    assert failure and isinstance(failure[0], RealStoreWriteBlocked)
    assert not target.exists()


@pytest.mark.parametrize("root", _REAL_ROOTS, ids=lambda r: str(r))
def test_sqlite_connect_for_write_is_refused(root):
    # sqlite opens its file from C and emits no `open` event; `sqlite3.connect`
    # is the only signal, and it is how production writes `~/.openclaw`.
    import sqlite3

    target = root / f"guard-{uuid.uuid4().hex}.sqlite"
    with pytest.raises(RealStoreWriteBlocked):
        sqlite3.connect(str(target))
    with pytest.raises(RealStoreWriteBlocked):
        sqlite3.connect(f"file:{target}?mode=rwc", uri=True)
    assert not target.exists()


def test_sqlite_read_only_uri_is_allowed():
    # Production reads the real OpenClaw store through `mode=ro`; that must
    # reach sqlite (and fail there, the file being absent) rather than the guard.
    import sqlite3

    target = _REAL_ROOTS[0] / f"guard-{uuid.uuid4().hex}.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    assert not target.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS is case-insensitive; Linux is not")
def test_a_differently_cased_spelling_is_refused_on_macos():
    root = _REAL_ROOTS[0]
    target = root.parent / root.name.upper() / f"guard-{uuid.uuid4().hex}"
    with pytest.raises(RealStoreWriteBlocked):
        open(target, "w")
    assert not target.exists()


@pytest.mark.parametrize("call", ["utime", "chown"])
def test_metadata_mutations_are_refused(call):
    target = _REAL_ROOTS[0] / f"guard-{uuid.uuid4().hex}"
    with pytest.raises(RealStoreWriteBlocked):
        if call == "utime":
            os.utime(target, (0, 0))
        else:
            os.chown(target, os.getuid(), os.getgid())
