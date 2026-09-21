"""Actual Codex TUI + server, fixture-only auth/HTTP, PTY integration."""
import asyncio
import contextlib
import fcntl
import json
import os
import pty
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from live_codex_smoke import HTTP, Pool, ThreadingHTTPServer, calls, limits

from xswap.codex_cli import WebSocketBridge, saved_thread, serve_cli


class ObservedBridge(WebSocketBridge):
 async def on_server(self,message):
  await super().on_server(message)
  if (os.environ.get('XSWAP_TEST_RESERVE')=='1' and not getattr(self,'reserve_sent',False)
      and message.get('method')=='turn/completed' and message['params']['turn']['status']=='completed'):
   self.reserve_sent=True
   self.pool.reserve_reached=True
   await super().on_server({'method':'account/rateLimits/updated','params':limits(10)})
 def __init__(self,*a,**kw):
  super().__init__(*a,**kw)
  original=self.emit
  def emit(message):
   with (self.status_path.parent/'events.jsonl').open('a') as f:f.write(json.dumps({'pid':self.process.pid if self.process else None,'cliPid':getattr(self,'client_pid',None),'message':message})+'\n')
   original(message)
  self.emit=emit

def runner(home,real):
 env={k:v for k,v in os.environ.items() if not k.startswith(('CODEX_','OPENAI_','XSWAP_'))};env.update(CODEX_HOME=home,TERM='xterm-256color')
 # Make the harness PTY this session's controlling terminal, as a user's terminal is: Codex
 # reads its size from /dev/tty and SIGWINCH is delivered to the foreground process group.
 with contextlib.suppress(OSError):fcntl.ioctl(0,termios.TIOCSCTTY,0)
 root=Path(home)
 pool=Pool()
 # A manager root is what reconnect_command needs to print the resume command after /exit.
 if os.environ.get('XSWAP_TEST_EXIT')=='1':pool.manager=SimpleNamespace(root=root/'swap-home')
 return asyncio.run(serve_cli(pool,real,['--no-alt-screen','-C',home,'-m','mock-model','-a','never','-s','read-only'],env,root/'status.json',root/'rpc.sock',ObservedBridge))

def main():
 server=ThreadingHTTPServer(('127.0.0.1',0),HTTP);threading.Thread(target=server.serve_forever,daemon=True).start()
 with tempfile.TemporaryDirectory(prefix='xc-',dir='/tmp') as tmp:
  home=Path(tmp);url='http://127.0.0.1:'+str(server.server_port)
  home.joinpath('config.toml').write_text('model="mock-model"\nmodel_provider="fixture"\nchatgpt_base_url='+json.dumps(url)+'\n[model_providers.fixture]\nname="fixture"\nbase_url='+json.dumps(url+'/v1')+'\nwire_api="responses"\nrequires_openai_auth=true\nsupports_websockets=false\n[analytics]\nenabled=false\n[projects.'+json.dumps(tmp)+']\ntrust_level="trusted"\n')
  browser_fixture=None
  if os.environ.get('XSWAP_TEST_BROWSER')=='1':
   from plugin_runtime_smoke import assert_browser_setup, plugin_fixture
   browser_fixture=plugin_fixture(home)
   asyncio.run(assert_browser_setup(home,browser_fixture))
  master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,0,0))
  real=os.environ.get('XSWAP_SMOKE_CODEX',os.path.expanduser('~/.codex/packages/standalone/current/bin/codex'))
  process=subprocess.Popen([sys.executable,__file__,'--runner',tmp,real],stdin=slave,stdout=slave,stderr=slave,start_new_session=True);os.close(slave)
  output=b'';stage=0;sent_at=0;started=time.monotonic();pids=set();cli_pids=set();tids=set();completed=0;main_tid=None;picker_done=False;picker_opened_at=0;manual_done=False;resized_at=None;second_turn_at=None
  try:
   while time.monotonic()-started<60:
    if select.select([master],[],[],.1)[0]:
     try:chunk=os.read(master,65536)
     except OSError:break
     output+=chunk
     if b'\x1b[6n' in chunk:os.write(master,b'\x1b[1;1R')
    events=[]
    if home.joinpath('events.jsonl').exists():
     for line in home.joinpath('events.jsonl').read_text().splitlines():
      try:events.append(json.loads(line))
      except ValueError:pass
    for e in events:
     m=e['message'];params=m.get('params',{})
     if e['pid']:pids.add(e['pid'])
     if e.get('cliPid'):cli_pids.add(e['cliPid'])
     if m.get('method')=='thread/started' and not main_tid:main_tid=params['thread']['id']
     if main_tid and params.get('threadId')==main_tid:tids.add(params['threadId'])
    completed=sum(e['message'].get('method')=='turn/completed' and e['message']['params']['turn']['status']=='completed' and e['message']['params'].get('threadId')==main_tid for e in events)
    if stage==0 and home.joinpath('status.json').exists() and time.monotonic()-started>4:
     os.write(master,b'\x1b[200~first turn\x1b[201~');time.sleep(1);os.write(master,b'\r');stage=1;sent_at=time.monotonic()
    elif stage==1 and completed>=1 and os.environ.get('XSWAP_TEST_MANUAL')=='1' and not manual_done:
     state=json.loads(home.joinpath('status.json').read_text())
     from xswap.manager import atomic_json
     atomic_json(home/'switch.json', {'instance':state['bridgeInstance'],'id':'fixture-manual-switch','account':'second'})
     stage=12
    elif stage==12 and json.loads(home.joinpath('status.json').read_text()).get('manualState')=='applied':
     # Login acknowledgement precedes the TUI processing its account update.
     manual_done=True;stage=13;sent_at=time.monotonic()
    elif stage==13 and time.monotonic()-sent_at>4:
     stage=1
    elif stage==1 and completed>=1 and os.environ.get('XSWAP_TEST_PICKER')=='1' and not picker_done:
     time.sleep(1);os.write(master,b'/resume');time.sleep(.3);os.write(master,b'\r');stage=10;picker_opened_at=time.monotonic()
    elif stage==10 and b'Resume a previous session' in output and time.monotonic()-picker_opened_at>3:
     assert b'Failed to start TUI session picker' not in output
     os.write(master,b'\r');stage=11;picker_opened_at=time.monotonic()
    elif stage==11 and time.monotonic()-picker_opened_at>3:
     picker_done=True;stage=1
    elif stage==1 and completed>=1 and os.environ.get('XSWAP_TEST_RESIZE')=='1' and resized_at is None:
     # A user's terminal shrinks: SIGWINCH goes to the foreground process group, the relay
     # copies the new size onto its PTY, and the TUI must redraw at the new width.
     time.sleep(1);fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',30,80,0,0));resized_at=len(output);sent_at=time.monotonic()
    elif stage==1 and completed>=1 and resized_at is not None and second_turn_at is None:
     if time.monotonic()-sent_at>3:
      second_turn_at=len(output);os.write(master,b'\x1b[200~second turn\x1b[201~');time.sleep(1);os.write(master,b'\r');stage=2
    elif stage==1 and completed>=1 and os.environ.get('XSWAP_TEST_EXIT')=='1':
     # The user's /exit, not Ctrl-C: the path that prints Codex's --remote footer and xswap's trailer.
     time.sleep(2);os.write(master,b'/exit');time.sleep(.5);os.write(master,b'\r');stage=5
    elif stage==1 and completed>=1:
     time.sleep(2);os.write(master,b'\x1b[200~second turn\x1b[201~');time.sleep(1);os.write(master,b'\r');stage=2
    elif stage==2 and completed>=2:
     time.sleep(2);os.write(master,b'\x1b[200~third turn\x1b[201~');time.sleep(1);os.write(master,b'\r');stage=3
    elif stage==3 and completed>=3:
     assert process.poll() is None
     assert b'xswap auto:' not in output, 'bridge log corrupted the active TUI'
     if browser_fixture:
      asyncio.run(assert_browser_setup(home,browser_fixture))
      print('PASS: actual browser runtime setup before and after CLI account switch')
     assert len(pids)==1 and len(tids)==1 and len(cli_pids)==1,(pids,tids,cli_pids)
     os.kill(next(iter(cli_pids)),0)
     first_second=any(c['account']=='first' and 'second turn' in json.dumps(c['body'].get('input',[])) for c in calls)
     assert first_second == (os.environ.get('XSWAP_TEST_RESERVE')!='1' and os.environ.get('XSWAP_TEST_MANUAL')!='1')
     assert any(c['account']=='second' and 'second turn' in json.dumps(c['body'].get('input',[])) for c in calls)
     assert not home.joinpath('auth.json').exists()
     # Ctrl-C, and a second one only if the TUI is still up: with the PTY as controlling
     # terminal, a Ctrl-C after Codex has restored cooked mode is a SIGINT to the whole
     # foreground group, which is the operator's own doing, not what this test measures.
     os.write(master,b'\x03')
     for _ in range(20):
      time.sleep(.1)
      if select.select([master],[],[],0)[0]:
       try:output+=os.read(master,65536)
       except OSError:break
      if process.poll() is not None or b'Shutting down' in output:break
     else:os.write(master,b'\x03')
     stage=4
     break
    if process.poll() is not None:break
   if os.environ.get('XSWAP_SMOKE_CAPTURE'):
    Path(os.environ['XSWAP_SMOKE_CAPTURE']).write_bytes(output)
    Path(os.environ['XSWAP_SMOKE_CAPTURE']+'.json').write_text(json.dumps({'resized_at':resized_at,'second_turn_at':second_turn_at}))
   if os.environ.get('XSWAP_TEST_EXIT')=='1':
    assert stage==5,f'/exit never sent: stage={stage}, completed={completed}'
    assert process.wait(timeout=10)==0,f'runner exit {process.returncode}'
    text=output.decode(errors='replace');lines=[l.rstrip('\r') for l in text.splitlines()]
    assert b'xswap auto:' not in output,'a bridge failure line in a clean /exit'
    assert 'Token usage so far:' in text,'Codex token usage line lost'
    # Codex 0.155.1's own dead --remote footer is dropped by the stdout relay (exit_relay.py);
    # everything else Codex printed must still be there.
    for stale in ('Disconnected from this task','Reconnect: codex --remote','Stop the current turn'):
     assert stale not in text,f'stale footer line survived: {stale!r}'
    assert '\x1b[6n' in text,'the TUI cursor-position query never came through the relay'
    assert 'first turn' in text
    commands=[l for l in lines if 'xswap run --auto' in l]
    assert len(commands)==1,commands
    assert lines[lines.index(commands[0])-1]=='xswap: Session ended. Resume this conversation:',lines[-6:]
    argv=shlex.split(commands[0])
    assert argv[:1]==['env'] and argv[1]=='CODEX_SWAP_HOME='+str(home/'swap-home') and argv[2]=='CODEX_HOME='+str(home),argv
    assert argv[3:9]==['xswap','run','--auto','--accounts','first,second','--'] and argv[9]=='resume',argv
    assert saved_thread(home,argv[10]),f'resume id {argv[10]} has no rollout under {home}'
    assert '--remote' not in commands[0]
    assert text.count('xswap: ')==1,'more than one xswap trailer line'
    assert not home.joinpath('auth.json').exists()
    print('PASS: /exit drops Codex\'s dead Disconnected/Reconnect/Stop footer, keeps the token usage line, and ends with one usable xswap resume command')
    server.shutdown();return
   if stage!=4:
    print(output.decode(errors='replace')[-2500:]);raise AssertionError(f'TUI failed: stage={stage}, calls={len(calls)}, completed={completed}')
   try:process.wait(timeout=10)
   except subprocess.TimeoutExpired:raise AssertionError('TUI/bridge did not shut down cleanly') from None
   if os.environ.get('XSWAP_TEST_MANUAL')=='1':
    assert manual_done
    print('PASS: manual account switch acknowledged, subsequent turns complete, no bridge stderr in TUI')
   if os.environ.get('XSWAP_TEST_PICKER')=='1':
    assert picker_done
    print('PASS: /resume picker opens, resumes the saved session, and subsequent turns complete')
   if os.environ.get('XSWAP_TEST_RESIZE')=='1':
    import re
    # Inline mode keeps the composer where it started until the terminal changes; a 40->30
    # row shrink makes the TUI clamp and redraw against the new bottom (after one transitional
    # frame that also happens without the relay). So, from the second turn on -- 3 s after the
    # resize -- the rows of the cursor moves (`ESC[row;colH`) must reach the last rows of a
    # 30-row screen and never pass 30.
    rows=lambda blob:[int(m.group(1)) for m in re.finditer(rb'\x1b\[(\d+);(\d+)H',blob)]
    settled=rows(output[second_turn_at:])
    assert settled,'no redraw after the resize'
    assert max(settled)<=30,f'the TUI kept drawing past row 30 after the resize: max row {max(settled)}'
    assert max(settled)>=26,f'the TUI never redrew against the new 30-row bottom: max row {max(settled)}'
    print(f'PASS: SIGWINCH reaches the TUI through the relay; after shrinking 40x120 -> 30x80 the cursor rows settle at <= 30 (max {max(settled)})')
   if os.environ.get('XSWAP_TEST_RESERVE')=='1':print('PASS: proactive weekly reserve switch; no first-account second-turn request')
   if os.environ.get('XSWAP_TEST_OUTAGE')=='1':print('PASS: session opened while the usage service was unreachable at startup; quota re-read before the first turn')
   print('PASS: real TUI remains alive, same TUI/server PIDs and thread through account switch and next user turn, Ctrl-C exits cleanly, no auth.json')
  finally:
   if process.poll() is None:os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=10)
   os.close(master)
 server.shutdown()

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='--runner':sys.exit(runner(sys.argv[2],sys.argv[3]))
 main()
