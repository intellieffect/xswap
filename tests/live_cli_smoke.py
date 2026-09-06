"""Actual Codex TUI + server, fixture-only auth/HTTP, PTY integration."""
import asyncio, fcntl, json, os, pty, select, signal, struct, subprocess, sys, tempfile, threading, time, termios
from pathlib import Path
from live_codex_smoke import HTTP, Pool, calls, ThreadingHTTPServer, limits
from xswap_cli import serve_cli, WebSocketBridge

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
 root=Path(home)
 return asyncio.run(serve_cli(Pool(),real,['--no-alt-screen','-C',home,'-m','mock-model','-a','never','-s','read-only'],env,root/'status.json',root/'rpc.sock',ObservedBridge))

def main():
 server=ThreadingHTTPServer(('127.0.0.1',0),HTTP);threading.Thread(target=server.serve_forever,daemon=True).start()
 with tempfile.TemporaryDirectory(prefix='xc-',dir='/tmp') as tmp:
  home=Path(tmp);url='http://127.0.0.1:'+str(server.server_port)
  home.joinpath('config.toml').write_text('model="mock-model"\nmodel_provider="fixture"\nchatgpt_base_url='+json.dumps(url)+'\n[model_providers.fixture]\nname="fixture"\nbase_url='+json.dumps(url+'/v1')+'\nwire_api="responses"\nrequires_openai_auth=true\nsupports_websockets=false\n[analytics]\nenabled=false\n[projects.'+json.dumps(tmp)+']\ntrust_level="trusted"\n')
  browser_fixture=None
  if os.environ.get('XSWAP_TEST_BROWSER')=='1':
   from plugin_runtime_smoke import plugin_fixture,assert_browser_setup
   browser_fixture=plugin_fixture(home)
   asyncio.run(assert_browser_setup(home,browser_fixture))
  master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,0,0))
  real=os.environ.get('XSWAP_SMOKE_CODEX',os.path.expanduser('~/.codex/packages/standalone/current/bin/codex'))
  process=subprocess.Popen([sys.executable,__file__,'--runner',tmp,real],stdin=slave,stdout=slave,stderr=slave,start_new_session=True);os.close(slave)
  output=b'';stage=0;sent_at=0;started=time.monotonic();pids=set();cli_pids=set();tids=set();completed=0;main_tid=None
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
    elif stage==1 and completed>=1:
     time.sleep(2);os.write(master,b'\x1b[200~second turn\x1b[201~');time.sleep(1);os.write(master,b'\r');stage=2
    elif stage==2 and completed>=2:
     time.sleep(2);os.write(master,b'\x1b[200~third turn\x1b[201~');time.sleep(1);os.write(master,b'\r');stage=3
    elif stage==3 and completed>=3:
     assert process.poll() is None
     if browser_fixture:
      asyncio.run(assert_browser_setup(home,browser_fixture))
      print('PASS: actual browser runtime setup before and after CLI account switch')
     assert len(pids)==1 and len(tids)==1 and len(cli_pids)==1,(pids,tids,cli_pids)
     os.kill(next(iter(cli_pids)),0)
     first_second=any(c['account']=='first' and 'second turn' in json.dumps(c['body'].get('input',[])) for c in calls)
     assert first_second == (os.environ.get('XSWAP_TEST_RESERVE')!='1')
     assert any(c['account']=='second' and 'second turn' in json.dumps(c['body'].get('input',[])) for c in calls)
     assert not home.joinpath('auth.json').exists()
     os.write(master,b'\x03');time.sleep(.3);os.write(master,b'\x03');stage=4
     break
    if process.poll() is not None:break
   if stage!=4:
    print(output.decode(errors='replace')[-2500:]);raise AssertionError(f'TUI failed: stage={stage}, calls={len(calls)}, completed={completed}')
   try:process.wait(timeout=10)
   except subprocess.TimeoutExpired:raise AssertionError('TUI/bridge did not shut down cleanly')
   if os.environ.get('XSWAP_TEST_RESERVE')=='1':print('PASS: proactive weekly reserve switch; no first-account second-turn request')
   print('PASS: real TUI remains alive, same TUI/server PIDs and thread through account switch and next user turn, Ctrl-C exits cleanly, no auth.json')
  finally:
   if process.poll() is None:os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=10)
   os.close(master)
 server.shutdown()

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='--runner':sys.exit(runner(sys.argv[2],sys.argv[3]))
 main()
