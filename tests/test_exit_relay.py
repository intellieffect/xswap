"""exit_relay: the stdout PTY relay and the exact-footer filter that rides on it."""
import asyncio
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import unittest

from xswap.providers.codex.exit_relay import (
    MARKER,
    FooterFilter,
    StdoutRelay,
    relay_wanted,
)

SOCK = '/tmp/xs-abc123/rpc.sock'
UUID = '01a0c132-7da3-76c2-963a-a269644cebca'
L1 = b'Disconnected from this task. Any running work continues.\n'
L2 = f'Reconnect: codex --remote unix://{SOCK} resume {UUID}\n'.encode()
L3 = f'Stop the current turn: run codex --remote unix://{SOCK} agents, select this task, and press ctrl + x.\n'.encode()
TOKENS = b'Token usage so far: total=6 input=3 output=3\n'
TUI = b'\x1b[?2004h\x1b[28;1H\x1b[J\x1b[2mShutting down...\x1b[22m\x1b[?25h\x1b[0 q'


def run(filter_, chunks):
    out = b''.join(filter_.feed(c) for c in chunks)
    return out + filter_.flush()


class FooterFilterTests(unittest.TestCase):
    def test_exact_block_is_dropped_and_everything_around_it_kept(self):
        f = FooterFilter(SOCK)
        self.assertEqual(run(f, [TUI + L1 + L2 + L3 + TOKENS]), TUI + TOKENS)
        self.assertEqual(f.dropped, 1)

    def test_one_byte_at_a_time_gives_the_same_result(self):
        stream = TUI + L1 + L2 + L3 + TOKENS
        f = FooterFilter(SOCK)
        self.assertEqual(run(f, [bytes([b]) for b in stream]), TUI + TOKENS)

    def test_every_split_point_gives_the_same_result(self):
        stream = TUI + L1 + L2 + L3 + TOKENS
        for i in range(len(stream)):
            f = FooterFilter(SOCK)
            self.assertEqual(run(f, [stream[:i], stream[i:]]), TUI + TOKENS, i)

    def test_colored_reconnect_and_stop_commands(self):
        l2 = f'Reconnect: \x1b[36mcodex --remote unix://{SOCK} resume {UUID}\x1b[39m\n'.encode()
        l3 = (f'Stop the current turn: run \x1b[36mcodex --remote unix://{SOCK} agents\x1b[39m, '
              'select this task, and press ctrl + x.\n').encode()
        self.assertEqual(run(FooterFilter(SOCK), [L1 + l2 + l3 + TOKENS]), TOKENS)

    def test_turn_interrupted_variant_has_no_stop_line_and_is_settled_by_the_next_line(self):
        l1 = b'Disconnected from this task. The current turn was stopped.\n'
        f = FooterFilter(SOCK)
        self.assertEqual(run(f, [l1 + L2, b'Token usage: total=1 input=1 output=0\n']),
                         b'Token usage: total=1 input=1 output=0\n')
        self.assertEqual(f.dropped, 1)

    def test_stop_less_block_at_end_of_stream_is_dropped_by_flush(self):
        f = FooterFilter(SOCK)
        self.assertEqual(f.feed(L1 + L2), b'')  # could still be followed by the Stop line
        self.assertEqual(f.flush(), b'')
        self.assertEqual(f.dropped, 1)

    def test_fatal_variant(self):
        l1 = b'Disconnected from this task. Work may still be running.\n'
        self.assertEqual(run(FooterFilter(SOCK), [l1 + L2 + L3]), b'')

    def test_another_sockets_footer_passes_through_untouched(self):
        other = L1 + L2.replace(b'xs-abc123', b'xs-other') + L3.replace(b'xs-abc123', b'xs-other') + TOKENS
        f = FooterFilter(SOCK)
        self.assertEqual(run(f, [other]), other)
        self.assertEqual(f.dropped, 0)

    def test_malformed_resume_id_passes_through_untouched(self):
        for bad in (UUID.upper(), UUID[:-1], UUID + 'f', UUID.replace('-', '_', 1)):
            block = L1 + L2.replace(UUID.encode(), bad.encode()) + L3 + TOKENS
            self.assertEqual(run(FooterFilter(SOCK), [block]), block, bad)

    def test_different_stop_hint_keeps_the_first_two_lines_dropped_only_when_exact(self):
        # A Stop line with an unknown key hint is not part of the known block: the block
        # without a Stop line still matches exactly, the odd Stop line is ordinary output.
        odd = L3.replace(b'ctrl + x', b'ctrl + q')
        self.assertEqual(run(FooterFilter(SOCK), [L1 + L2 + odd + TOKENS]), odd + TOKENS)

    def test_partial_block_cut_off_by_end_of_stream_is_given_back_verbatim(self):
        cut = L1 + L2[:20]
        f = FooterFilter(SOCK)
        self.assertEqual(f.feed(TUI + cut), TUI)
        self.assertEqual(f.flush(), cut)

    def test_marker_inside_ordinary_output_is_not_held_beyond_the_mismatch(self):
        text = b'echo "' + MARKER + b'nothing"\nmore\n'
        f = FooterFilter(SOCK)
        self.assertEqual(f.feed(text), text)
        self.assertEqual(f.pending, b'')

    def test_large_unterminated_output_flows_through_with_a_bounded_hold(self):
        data = bytes(range(256)) * 300  # 76800 bytes, no newline, never a marker prefix
        f = FooterFilter(SOCK)
        self.assertEqual(f.feed(data), data)
        tail = b'x' * 70000 + MARKER[:10]
        self.assertEqual(f.feed(tail), tail[:-10])
        self.assertEqual(f.pending, MARKER[:10])
        self.assertLess(len(f.pending), len(MARKER))
        self.assertEqual(f.flush(), MARKER[:10])

    def test_two_blocks_and_bytes_between_them(self):
        stream = b'a' + L1 + L2 + L3 + b'b' + L1 + L2 + L3 + b'c'
        f = FooterFilter(SOCK)
        self.assertEqual(run(f, [stream]), b'abc')
        self.assertEqual(f.dropped, 2)

    def test_crlf_output_is_not_the_block(self):
        # The slave is raw, so Codex's println gives bare LF; a CRLF stream is some other
        # writer and is left alone.
        crlf = (L1 + L2 + L3).replace(b'\n', b'\r\n')
        self.assertEqual(run(FooterFilter(SOCK), [crlf]), crlf)


class RelayWantedTests(unittest.TestCase):
    def test_tty_without_escape_hatch(self):
        master, slave = pty.openpty()
        try:
            self.assertTrue(relay_wanted(slave, {}))
            self.assertFalse(relay_wanted(slave, {'XSWAP_RAW_EXIT': '1'}))
            self.assertTrue(relay_wanted(slave, {'XSWAP_RAW_EXIT': '0'}))
        finally:
            os.close(master); os.close(slave)

    def test_pipe_or_closed_fd_means_inherit(self):
        r, w = os.pipe()
        try:
            self.assertFalse(relay_wanted(w, {}))
        finally:
            os.close(r); os.close(w)
        self.assertFalse(relay_wanted(w, {}))  # closed: OSError, not a crash


def closed(fd):
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


class StdoutRelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r, self.w = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (self.r, self.w) if not closed(fd)])

    def collected(self):
        os.close(self.w)
        chunks = []
        while True:
            chunk = os.read(self.r, 65536)
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)

    async def spawn_through(self, relay, script):
        slave = relay.open()
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, stdout=slave)
        relay.start(asyncio.get_running_loop())
        return process

    async def test_child_output_reaches_out_fd_minus_the_footer_and_fds_are_released(self):
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=self.w)
        block = (L1 + L2 + L3).decode()
        script = f'import sys; sys.stdout.write({TUI.decode()!r} + {block!r} + {TOKENS.decode()!r}); sys.stdout.flush()'
        process = await self.spawn_through(relay, script)
        self.assertEqual(await process.wait(), 0)
        master, slave = relay.master, relay.slave
        relay.close()
        self.assertEqual(self.collected(), TUI + TOKENS)
        self.assertEqual(relay.filter.dropped, 1)
        self.assertTrue(closed(master) and closed(slave))
        self.assertIsNone(relay.master); self.assertIsNone(relay.slave)

    async def test_last_bytes_survive_a_loop_that_never_got_to_read_them(self):
        # macOS drops what is queued on a PTY master once the last slave closes; the footer and
        # token line are Codex's last bytes. The relay holds a slave, so a drain after the
        # child's exit still finds them even if the reader never ran while the child lived.
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=self.w)
        slave = relay.open()
        block = (L1 + L2 + L3).decode()
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', f'import sys; sys.stdout.write({block!r} + {TOKENS.decode()!r})', stdout=slave)
        self.assertEqual(await process.wait(), 0)  # exited before start(): nothing has read the master
        relay.start(asyncio.get_running_loop())
        relay.close()
        self.assertEqual(self.collected(), TOKENS)

    async def test_unterminated_output_is_relayed_before_the_child_exits(self):
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=self.w)
        script = 'import sys, time; sys.stdout.write("prompt> "); sys.stdout.flush(); time.sleep(1.5)'
        process = await self.spawn_through(relay, script)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if fcntl.ioctl(self.r, termios.FIONREAD, b'\0\0\0\0') != b'\0\0\0\0':
                break
        self.assertEqual(os.read(self.r, 100), b'prompt> ')  # before the child is anywhere near exit
        await process.wait()
        relay.close()

    async def test_slave_is_raw_and_sized_from_out_fd(self):
        out_master, out_slave = pty.openpty()
        self.addCleanup(lambda: [os.close(fd) for fd in (out_master, out_slave) if not closed(fd)])
        fcntl.ioctl(out_slave, termios.TIOCSWINSZ, struct.pack('HHHH', 33, 77, 0, 0))
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=out_slave)
        slave = relay.open()
        attrs = termios.tcgetattr(slave)
        self.assertFalse(attrs[1] & termios.OPOST)  # bytes reach the real terminal as written
        self.assertFalse(attrs[3] & termios.ECHO)
        self.assertEqual(struct.unpack('HHHH', fcntl.ioctl(slave, termios.TIOCGWINSZ, b'\0' * 8))[:2], (33, 77))
        fcntl.ioctl(out_slave, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 60, 0, 0))
        relay.sync_winsize()
        self.assertEqual(struct.unpack('HHHH', fcntl.ioctl(slave, termios.TIOCGWINSZ, b'\0' * 8))[:2], (20, 60))
        relay.abort()
        self.assertTrue(closed(slave))

    async def test_sigwinch_resizes_the_pty_while_running_and_the_handler_is_restored_after(self):
        before = signal.getsignal(signal.SIGWINCH)
        out_master, out_slave = pty.openpty()
        self.addCleanup(lambda: [os.close(fd) for fd in (out_master, out_slave) if not closed(fd)])
        fcntl.ioctl(out_slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=out_slave)
        process = await self.spawn_through(relay, 'import time; time.sleep(0.5)')
        size = lambda: struct.unpack('HHHH', fcntl.ioctl(relay.master, termios.TIOCGWINSZ, b'\0' * 8))[:2]
        self.assertEqual(size(), (40, 120))
        fcntl.ioctl(out_slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 80, 0, 0))
        os.kill(os.getpid(), signal.SIGWINCH)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if size() == (30, 80):
                break
        self.assertEqual(size(), (30, 80))
        await process.wait()
        relay.close()
        self.assertEqual(signal.getsignal(signal.SIGWINCH), before)

    async def test_spawn_failure_path_releases_both_fds(self):
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=self.w)
        slave = relay.open()
        master = relay.master
        with self.assertRaises(FileNotFoundError):
            try:
                await asyncio.create_subprocess_exec('/nonexistent/xswap-fixture-codex', stdout=slave)
            except BaseException:
                relay.abort()
                raise
        self.assertTrue(closed(slave) and closed(master))
        self.assertIsNone(relay.master)
        self.assertEqual(self.collected(), b'')

    async def test_abort_mid_stream_gives_back_held_bytes_and_leaves_no_reader(self):
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=self.w)
        cut = (L1 + L2[:10]).decode()
        process = await self.spawn_through(relay, f'import sys, time; sys.stdout.write({cut!r}); sys.stdout.flush(); time.sleep(1)')
        for _ in range(100):
            await asyncio.sleep(0.02)
            if relay.filter.pending:
                break
        self.assertEqual(relay.filter.pending, L1 + L2[:10])
        master = relay.master
        relay.abort()
        self.assertTrue(closed(master))
        process.terminate(); await process.wait()
        self.assertEqual(self.collected(), L1 + L2[:10])

    async def test_close_does_not_wait_for_a_grandchild_that_keeps_the_slave(self):
        relay = StdoutRelay(FooterFilter(SOCK), out_fd=self.w)
        script = ('import subprocess, sys; sys.stdout.write("hello"); sys.stdout.flush(); '
                  'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])')
        process = await self.spawn_through(relay, script)
        await process.wait()
        loop = asyncio.get_running_loop()
        started = loop.time()
        relay.close()
        self.assertLess(loop.time() - started, 1)
        self.assertIsNone(relay.master)
        self.assertEqual(self.collected(), b'hello')
