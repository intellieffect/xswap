"""Relay the Codex TUI's stdout through a PTY so its dead `--remote` exit footer can be dropped.

Codex 0.155 ends every `--remote` TUI by printing, on stdout and with no switch to turn it off,
"Disconnected from this task. Any running work continues.", a `Reconnect: codex --remote
unix:///tmp/xs-…/rpc.sock resume ID` line and a `Stop the current turn: … agents …` line
(`codex-rs/tui/src/app/exit_summary.rs`). Under xswap the socket directory is deleted and the
bridge stops its app-server when the TUI leaves, so all three lines are wrong. The TUI refuses
to start unless stdout is a terminal (probed 2026-09-21: `Error: stdout is not a terminal`), so
a pipe is not an option; what works is a pseudo-terminal whose slave becomes the TUI's stdout
while stdin and stderr stay the user's own terminal. Everything the TUI writes is copied to
the real stdout byte for byte, as soon as it is read -- ANSI, partial lines, the token-usage
line, errors -- except the one exact footer block for this run's socket, which is dropped.

The slave is put in raw mode so the bytes reaching the real terminal are the same bytes Codex
wrote; the real terminal's own line discipline (which Codex switches in and out of raw mode
through /dev/tty, not through stdout) still applies them. The slave's window size is copied
from the real stdout at start and on every SIGWINCH.

Only stdout moves. stdin stays inherited, so key input, job control and Ctrl-C reach the TUI
exactly as before, and the TUI's terminal queries (`\\x1b[6n`, keyboard-protocol probes) go out
through the relay and are answered on the inherited stdin. `XSWAP_RAW_EXIT=1` disables the
relay and hands the TUI the real stdout again, footer and all.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import signal
import struct
import termios
import tty
from collections.abc import Callable
from typing import Any

#: Escape hatch: skip the relay and give the TUI the real stdout (footer included).
RAW_EXIT_VARIABLE = 'XSWAP_RAW_EXIT'

#: The three first lines `format_exit_messages` can print, all followed by the same
#: Reconnect/Stop lines and all false under xswap. Matched exactly.
_MESSAGES = (
    b'Disconnected from this task. Any running work continues.',
    b'Disconnected from this task. Work may still be running.',
    b'Disconnected from this task. The current turn was stopped.',
)
#: Every message starts with this; it is what the scanner looks for in the stream.
MARKER = b'Disconnected from this task. '
_COLOR_ON, _COLOR_OFF = b'\x1b[36m', b'\x1b[39m'
_STOP_HINT = b'press ctrl + x'
#: How the Stop line starts. A block whose third line starts like this but is not the exact
#: known Stop line (or is cut off by the end of the stream) is not dropped at all.
_STOP_PREFIX = b'Stop the current turn:'
#: Placeholder for the thread id in a template: a v4/v7 UUID as Codex prints it.
_UUID = object()

Segment = bytes | object


def _templates(socket_path: str) -> list[list[Segment]]:
    """Every exact byte sequence the footer block can be for this run's socket.

    Longer templates (with the Stop line) come first so a complete match prefers them; a
    template without the Stop line is what `TurnInterrupted` prints and also what the
    scanner settles on when the bytes after the Reconnect line turn out not to be a Stop line.
    """
    socket = ('unix://' + socket_path).encode()
    out: list[list[Segment]] = []
    for with_stop in (True, False):
        for color in (True, False):
            on, off = (_COLOR_ON, _COLOR_OFF) if color else (b'', b'')
            for message in _MESSAGES:
                template: list[Segment] = [message + b'\nReconnect: ' + on + b'codex --remote ' + socket +
                                           b' resume ', _UUID, off + b'\n']
                if with_stop:
                    template.append(b'Stop the current turn: run ' + on + b'codex --remote ' + socket +
                                    b' agents' + off + b', select this task, and ' + _STOP_HINT + b'.\n')
                out.append(template)
    return out


#: A v4/v7 UUID as Codex prints it: lowercase hex in the 8-4-4-4-12 layout.
UUID_MASK = 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
UUID_LENGTH = len(UUID_MASK)


def _uuid_char_ok(index: int, char: int) -> bool:
    return char == ord('-') if UUID_MASK[index] == '-' else char in b'0123456789abcdef'


def _match(template: list[Segment], data: bytes) -> tuple[str, int]:
    """('complete', length) | ('partial', 0) | ('no', 0) for `data` against `template`."""
    pos = 0
    for segment in template:
        if segment is _UUID:
            for i in range(UUID_LENGTH):
                if pos >= len(data):
                    return 'partial', 0
                if not _uuid_char_ok(i, data[pos]):
                    return 'no', 0
                pos += 1
            continue
        assert isinstance(segment, bytes)  # noqa: S101 -- the only other segment kind is the _UUID sentinel handled above
        chunk = data[pos:pos + len(segment)]
        if not segment.startswith(chunk):
            return 'no', 0
        if len(chunk) < len(segment):
            return 'partial', 0
        pos += len(segment)
    return 'complete', pos


class FooterFilter:
    """Streaming byte filter: passes everything through except this run's exact footer block.

    `feed(data)` returns the bytes to write now; `flush()` returns whatever was being held
    back at end of stream. Hold-back is bounded by the longest template (a few hundred bytes):
    outside a candidate block at most `len(MARKER) - 1` bytes wait for the next chunk, so
    unterminated output of any size flows through as it arrives.

    Fail-open: a block is dropped only when it is exactly one of the known templates. A Stop
    line with another wording, or one cut off by the end of the stream, keeps the whole block
    (Disconnected and Reconnect lines included) on the terminal byte for byte.
    """

    def __init__(self, socket_path: str) -> None:
        self.templates = _templates(socket_path)
        self.pending = b''
        self.dropped = 0  #: footer blocks removed so far

    def _classify(self, candidate: bytes) -> tuple[str, int]:
        """A partial match outranks a complete one: the block without the Stop line is a
        prefix of the block with it, so the scanner waits for the next chunk (or the end of
        the stream, where `flush` settles it) rather than dropping half a footer.

        A Stop-less match followed by a Stop line that is not the exact known one is 'no':
        the whole block is passed through rather than the first two lines dropped, which
        would leave a Stop line with no Reconnect line above it to make sense of."""
        complete = ('no', 0)
        for template in self.templates:
            kind, length = _match(template, candidate)
            if kind == 'partial':
                return kind, 0
            if kind == 'complete' and length > complete[1]:
                complete = (kind, length)
        if complete[1] and not candidate[:complete[1]].endswith(_STOP_HINT + b'.\n') \
                and candidate[complete[1]:].startswith(_STOP_PREFIX):
            return 'no', 0
        return complete

    def feed(self, data: bytes) -> bytes:
        buf = self.pending + data
        out = bytearray()
        while True:
            i = buf.find(MARKER)
            if i == -1:
                keep = _suffix_prefix(buf, MARKER)
                out += buf[:len(buf) - keep]
                self.pending = buf[len(buf) - keep:]
                return bytes(out)
            out += buf[:i]
            candidate = buf[i:]
            kind, length = self._classify(candidate)
            if kind == 'complete':
                self.dropped += 1
                buf = candidate[length:]
                continue
            if kind == 'partial':
                self.pending = candidate
                return bytes(out)
            # Not this run's footer: the marker itself is ordinary output, keep scanning after it.
            out += candidate[:len(MARKER)]
            buf = candidate[len(MARKER):]

    def flush(self) -> bytes:
        """End of stream: a held candidate that is exactly a complete block (the Stop-less
        variant, nothing after it) is dropped; anything else held -- a block cut off inside
        the Reconnect line, or inside a Stop line that never completed -- is given back
        unchanged."""
        out, self.pending = self.pending, b''
        if out.startswith(MARKER) and any(_match(t, out) == ('complete', len(out)) for t in self.templates):
            self.dropped += 1
            return b''
        return out


def _suffix_prefix(data: bytes, marker: bytes) -> int:
    """Length of the longest suffix of `data` that is a proper prefix of `marker`."""
    for k in range(min(len(marker) - 1, len(data)), 0, -1):
        if data.endswith(marker[:k]):
            return k
    return 0


def relay_wanted(out_fd: int = 1, environ: dict[str, str] | None = None) -> bool:
    """Relay only when stdout is an interactive terminal and the escape hatch is not set."""
    env = os.environ if environ is None else environ
    if env.get(RAW_EXIT_VARIABLE) == '1':
        return False
    try:
        return os.isatty(out_fd)
    except OSError:
        return False


class StdoutRelay:
    """A PTY whose slave is the child's stdout; the master is copied to `out_fd` through a filter.

    Lifecycle: `open()` before spawning (returns the slave fd to pass as the child's stdout),
    `start(loop)` once it is running to begin copying, `close()` after the child has exited
    to drain what is left, emit anything the filter was holding, and release fds and the
    SIGWINCH handler. `abort()` is `close()` for the spawn-failed / cancelled paths.

    The parent keeps its own slave fd open until `close()`. macOS discards whatever is still
    queued on the master the moment the last slave closes (measured 2026-09-21), and the
    footer and token line are exactly what Codex writes last before exiting; with a slave held
    here they wait in the queue until the drain reads them, however busy the loop was.
    """

    def __init__(self, filter_: FooterFilter, out_fd: int = 1, write: Callable[[bytes], None] | None = None) -> None:
        self.filter = filter_
        self.out_fd = out_fd
        self._write = write or self._write_out
        self.master: int | None = None
        self.slave: int | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.eof = asyncio.Event()
        self._previous_winch: Any = None
        self._winch_installed = False
        self._reading = False

    def open(self) -> int:
        """Open the PTY and return the slave fd for the child's stdout.

        The pair is owned from the moment `openpty` returns: if anything after it fails
        (raw mode, non-blocking mode) both fds are closed again and the relay is left as it
        was before the call, so the caller has nothing to release.
        """
        self.master, self.slave = pty.openpty()
        try:
            tty.setraw(self.slave, termios.TCSANOW)
            os.set_blocking(self.master, False)
        except BaseException:
            self._release()
            raise
        self.sync_winsize()
        return self.slave

    def sync_winsize(self) -> None:
        """Copy the real terminal's window size onto the PTY (initially and on SIGWINCH)."""
        if self.master is None:
            return
        try:
            size = fcntl.ioctl(self.out_fd, termios.TIOCGWINSZ, struct.pack('HHHH', 0, 0, 0, 0))
        except OSError:
            return
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        assert self.master is not None  # noqa: S101 -- start() is only reached after open()
        self.loop = loop
        loop.add_reader(self.master, self._on_readable)
        self._reading = True
        with contextlib.suppress(AttributeError, ValueError, OSError, RuntimeError, NotImplementedError):
            self._previous_winch = signal.getsignal(signal.SIGWINCH)
            loop.add_signal_handler(signal.SIGWINCH, self.sync_winsize)
            self._winch_installed = True

    def _on_readable(self) -> None:
        assert self.master is not None  # noqa: S101 -- the reader is removed before the master is closed
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: every slave fd is gone, which cannot happen before close() here
            data = b''
        if not data:
            self._stop_reading()
            self._write(self.filter.flush())
            self.eof.set()
            return
        self._write(self.filter.feed(data))

    def _drain(self, limit: int = 256) -> None:
        """Copy whatever is queued on the master right now (synchronously, to EAGAIN or EOF).

        `limit` reads of 64 KiB bound the time spent here should a grandchild keep writing.
        """
        for _ in range(limit):
            if self.master is None:
                return
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                return
            except OSError:
                return
            if not data:
                return
            self._write(self.filter.feed(data))

    def _write_out(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.out_fd, view)
            except BlockingIOError:
                continue
            except OSError:
                return  # the real stdout is gone; nothing sensible left to do with the bytes
            view = view[n:]

    def _stop_reading(self) -> None:
        if self._reading and self.loop is not None and self.master is not None:
            with contextlib.suppress(Exception):
                self.loop.remove_reader(self.master)
        self._reading = False

    def _release(self) -> None:
        self._stop_reading()
        if self._winch_installed and self.loop is not None:
            with contextlib.suppress(Exception):
                self.loop.remove_signal_handler(signal.SIGWINCH)
            with contextlib.suppress(Exception):
                if self._previous_winch is not None:
                    signal.signal(signal.SIGWINCH, self._previous_winch)
            self._winch_installed = False
        for name in ('slave', 'master'):
            fd = getattr(self, name)
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, name, None)

    def close(self) -> None:
        """The child has exited: copy what it left in the queue, then release everything.

        Nothing here waits. The child's last writes are already queued (the write returned
        before it exited), so one drain reads them; closing the held slave then lets the
        master report EOF, and a second drain settles the filter. A grandchild still holding
        the slave just means the second drain stops at EAGAIN instead of EOF.
        """
        try:
            self._stop_reading()
            self._drain()
            if self.slave is not None:
                os.close(self.slave)
                self.slave = None
            self._drain()
            if not self.eof.is_set():
                self._write(self.filter.flush())
                self.eof.set()
        finally:
            self._release()

    def abort(self) -> None:
        """Synchronous release for the paths where the child never ran or the loop is going away."""
        if not self.eof.is_set():
            with contextlib.suppress(Exception):
                self._write(self.filter.flush())
            self.eof.set()
        self._release()
