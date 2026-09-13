"""Opt-in real model test: XSWAP_TEST_ACCOUNTS=first,second python tests/live_manual_switch.py.

Uses an isolated registry/runtime and closes only its own server. Two short real
model turns consume quota; source credentials may be refreshed by official Codex.
Never prints tokens, account IDs, or prompts beyond the fixed test response.
"""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import tempfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_swap import Manager, private_dir
from xswap_live import AccountPool, Bridge
from xswap_switch import switch_running

async def main():
    source = Manager()
    real = source.codex()
    with tempfile.TemporaryDirectory(prefix='xswap-verify-') as temporary:
        root = Path(temporary)
        manager = Manager(root/'store', root/'home')
        private_dir(manager.source)
        names = os.environ['XSWAP_TEST_ACCOUNTS'].split(',')
        if len(names) != 2 or names[0] == names[1]:
            raise ValueError('XSWAP_TEST_ACCOUNTS must name two distinct signed-in accounts')
        for name in names:
            manager.register(name, source.account(name)[1])
        directory = manager.root/'auto'/'cli-runs'/'verify'
        for path in (directory.parent.parent, directory.parent, directory):
            private_dir(path)
        fd = os.open(directory/'.bridge.lock', os.O_CREAT|os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        stop = asyncio.Event()
        events = asyncio.Queue()
        class Harness(Bridge):
            async def client_reader(self):
                await stop.wait()
        bridge = Harness(AccountPool(manager, names, real), [real, 'app-server', '--stdio'],
                         manager.env(manager.source), emit=events.put_nowait,
                         status_path=directory/'status.json')
        runner = asyncio.create_task(bridge.run())
        try:
            while bridge.process is None:
                await asyncio.sleep(.02)
            await bridge.on_client({'id':1,'method':'initialize','params':{
                'clientInfo':{'name':'xswap-verification','version':'1.0'},
                'capabilities':{'experimentalApi':True}}})
            models = await bridge.rpc('model/list', {})
            options = models.get('data', [])
            model = next((m['id'] for m in options if m.get('isDefault')), options[0]['id'])
            thread = await bridge.rpc('thread/start', {'cwd':temporary,'model':model,
                'approvalPolicy':'never','sandbox':'read-only','ephemeral':True})
            thread_id = thread['thread']['id']
            async def turn(request_id):
                await bridge.on_client({'id':request_id,'method':'turn/start','params':{
                    'threadId':thread_id, 'model':model,
                    'input':[{'type':'text','text':'Reply exactly XSWAP_ACCOUNT_OK. Do not use tools.'}]}})
                texts = []
                while True:
                    event = await asyncio.wait_for(events.get(), 90)
                    if event.get('id') == request_id and event.get('error'):
                        raise RuntimeError('turn/start rejected')
                    if event.get('method') == 'item/agentMessage/delta':
                        texts.append(event['params'].get('delta',''))
                    if event.get('method') == 'turn/completed':
                        status = event['params']['turn']['status']
                        if status != 'completed':
                            raise RuntimeError('model turn failed: '+status)
                        if 'XSWAP_ACCOUNT_OK' not in ''.join(texts):
                            raise RuntimeError('expected response absent')
                        return
            await turn(10)
            initial_pid = bridge.process.pid
            print(json.dumps({'stage':'before','account':bridge.current,'response':'XSWAP_ACCOUNT_OK'}),flush=True)
            report = await asyncio.to_thread(switch_running, manager, names[1], 15)
            if report['applied'] != 1:
                raise RuntimeError('switch not applied: '+str(report))
            expected = bridge.pool.prepare(names[1])[0]['chatgptAccountId']
            if bridge.current_id != expected:
                raise RuntimeError('account identity mismatch')
            if bridge.verified_account != names[1]:
                raise RuntimeError('server login not verified: ' + str(bridge.verify_reason))
            await turn(11)
            print(json.dumps({'stage':'after','account':bridge.current,'response':'XSWAP_ACCOUNT_OK',
                'sameServer':initial_pid==bridge.process.pid,'sameThread':True,'report':report}),flush=True)
        finally:
            stop.set()
            await runner
            os.close(fd)

try:
    asyncio.run(main())
except Exception as exc:
    print('Verification failed: '+type(exc).__name__+': '+str(exc)[:200],file=sys.stderr)
    sys.exit(1)
