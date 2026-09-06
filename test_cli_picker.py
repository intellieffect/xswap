import asyncio
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from xswap_cli import WebSocketBridge


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.replies = []
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return json.dumps(next(self.messages))
        except StopIteration:
            raise StopAsyncIteration
    async def send(self, text):
        self.replies.append(json.loads(text))


class PickerTests(IsolatedAsyncioTestCase):
    def bridge(self):
        bridge = WebSocketBridge(SimpleNamespace(names=['main']), [], {}, socket=None)
        bridge.booting = False
        bridge.initialized = True
        bridge.initialize_result = {'userAgent': 'fixture'}
        bridge.ready.set()
        return bridge

    async def test_picker_ids_are_isolated_and_main_notifications_stay_on_main(self):
        bridge = self.bridge()
        forwarded = []
        async def send(message):
            forwarded.append(message)
            await bridge.on_server({'method': 'thread/started', 'params': {'thread': {'id': 'main'}}})
            await bridge.on_server({'id': message['id'], 'result': {'data': []}})
        bridge.send = send
        sockets = [Socket([{'id': 0, 'method': 'initialize'},
                           {'method': 'initialized'}, {'id': 1, 'method': 'thread/list'}]) for _ in range(2)]
        await asyncio.gather(*(bridge.picker_client(socket) for socket in sockets))
        self.assertEqual(len({m['id'] for m in forwarded}), 2)
        self.assertTrue(all(m['id'] != 1 for m in forwarded))
        for socket in sockets:
            self.assertEqual(socket.replies, [{'id': 0, 'result': {'userAgent': 'fixture'}},
                                             {'id': 1, 'result': {'data': []}}])
        self.assertEqual(bridge.outbox.qsize(), 2)
        self.assertEqual(bridge.requests, {})
        self.assertFalse(bridge.stopping)

    async def test_picker_cannot_start_turns_or_change_authentication(self):
        bridge = self.bridge()
        socket = Socket([{'id': 0, 'method': 'initialize'},
                         {'id': 1, 'method': 'turn/start'},
                         {'id': 2, 'method': 'account/login/start'},
                         {'id': 3, 'method': 'initialize'}])
        await bridge.picker_client(socket)
        self.assertTrue(all('error' in r for r in socket.replies[1:]))
        self.assertEqual(bridge.requests, {})

    async def test_server_error_is_returned_to_picker_only(self):
        bridge = self.bridge()
        async def send(message):
            await bridge.on_server({'id': message['id'], 'error': {'code': -32602, 'message': 'bad cursor'}})
        bridge.send = send
        socket = Socket([{'id': 0, 'method': 'initialize'}, {'id': 1, 'method': 'thread/list'}])
        await bridge.picker_client(socket)
        self.assertEqual(socket.replies[-1]['error']['code'], -32602)
        self.assertTrue(bridge.outbox.empty())
        self.assertEqual(bridge.requests, {})

    async def test_cancelled_picker_does_not_leak_late_response_to_main(self):
        bridge = self.bridge()
        sent = asyncio.Event()
        request_ids = []
        async def send(message):
            request_ids.append(message['id'])
            sent.set()
        bridge.send = send
        socket = Socket([{'id': 0, 'method': 'initialize'}, {'id': 1, 'method': 'thread/list'}])
        task = asyncio.create_task(bridge.picker_client(socket))
        await sent.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await bridge.on_server({'id': request_ids[0], 'result': {'data': []}})
        self.assertEqual(bridge.requests, {})
        self.assertTrue(bridge.outbox.empty())
        await bridge.on_server({'id': 1, 'result': {'main': True}})
        self.assertEqual(bridge.outbox.get_nowait(), {'id': 1, 'result': {'main': True}})
