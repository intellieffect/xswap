"""Opt-in integration test using a real Codex binary and fake local HTTP/auth only.
Run: python3 tests/live_codex_smoke.py (optionally XSWAP_SMOKE_CODEX=/path/to/codex).
"""
from xswap_usage import UsageError
import asyncio,base64,contextlib,json,os,sys,tempfile,time,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from xswap_live import Bridge
from test_live import limits

calls=[]
def token(name):
 p={'sub':name,'email':name+'@example.test','exp':int(time.time())+3600,'https://api.openai.com/auth':{'chatgpt_account_id':name,'chatgpt_plan_type':'pro'}}
 enc=lambda x:base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip('=')
 return enc({'alg':'none'})+'.'+enc(p)+'.fake'
class HTTP(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
  with contextlib.suppress(BrokenPipeError,ConnectionResetError):self.wfile.write(b'{"models":[],"data":[]}')
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
  if self.path != '/v1/responses':
   self.send_response(404);self.end_headers();return
  name=self.headers.get('ChatGPT-Account-ID','missing')
  calls.append({'path':self.path,'account':name,'body':body})
  if name=='first' and 'second turn' in json.dumps(body.get('input', [])):
   data={'error':{'type':'usage_limit_reached','message':'fixture limit','plan_type':'pro','resets_at':int(time.time())+3600}}
   self.send_response(429);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(data).encode());return
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
  item={'type':'message','id':'msg-'+str(len(calls)),'role':'assistant','content':[{'type':'output_text','text':'fixture '+name,'annotations':[]}]}
  response={'id':'resp-'+str(len(calls)),'object':'response','status':'completed','output':[item],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
  for event in [{'type':'response.created','response':{'id':response['id'],'status':'in_progress','output':[]}}, {'type':'response.output_item.added','output_index':0,'item':{**item,'content':[]}}, {'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'fixture '+name}, {'type':'response.output_item.done','output_index':0,'item':item}, {'type':'response.completed','response':response}]:
   self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode());self.wfile.flush()

class Pool:
 names=['first','second']
 weekly_remaining=10 if os.environ.get('XSWAP_TEST_RESERVE')=='1' else 0
 outage=os.environ.get('XSWAP_TEST_OUTAGE')=='1'
 def prepare(self,name,require_quota=True):
  credentials={'accessToken':token(name),'chatgptAccountId':name,'chatgptPlanType':'pro'}
  if self.outage:
   # Usage service unreachable exactly once, at startup (2026-09-08 incident).
   self.outage=False
   if require_quota:raise UsageError('usage request timed out')
   return credentials,None
  return credentials,limits(10 if getattr(self,'reserve_reached',False) and name=='first' else 100)
 def refresh(self,name):return self.prepare(name)[0]

async def main():
 server=ThreadingHTTPServer(('127.0.0.1',0),HTTP);threading.Thread(target=server.serve_forever,daemon=True).start()
 with tempfile.TemporaryDirectory(prefix='xswap-fixture-') as tmp:
  home=Path(tmp);url='http://127.0.0.1:'+str(server.server_port)
  home.joinpath('config.toml').write_text('model="mock-model"\nmodel_provider="fixture"\nchatgpt_base_url='+json.dumps(url)+'\n[model_providers.fixture]\nname="fixture"\nbase_url='+json.dumps(url+'/v1')+'\nwire_api="responses"\nrequires_openai_auth=true\nsupports_websockets=false\n[analytics]\nenabled=false\n')
  browser_fixture=None
  if os.environ.get('XSWAP_TEST_BROWSER')=='1':
   from plugin_runtime_smoke import plugin_fixture,assert_browser_setup
   browser_fixture=plugin_fixture(home)
   await assert_browser_setup(home,browser_fixture)
  env={k:v for k,v in os.environ.items() if not k.startswith(('CODEX_','OPENAI_','XSWAP_'))};env['CODEX_HOME']=str(home)
  executable=os.environ.get('XSWAP_SMOKE_CODEX',os.path.expanduser('~/.local/bin/codex'))
  events=[]
  bridge=Bridge(Pool(),[executable,'app-server','--stdio'],env,events.append)
  bridge.process=await asyncio.create_subprocess_exec(*bridge.argv,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env,limit=32*1024*1024)
  reader=asyncio.create_task(bridge.server_reader())
  async def stderr():
   while await bridge.process.stderr.readline():
    pass  # Fixture backend intentionally lacks optional remote plugin APIs.
  err=asyncio.create_task(stderr())
  try:
   await bridge.on_client({'id':1,'method':'initialize','params':{'clientInfo':{'name':'xswap_fixture','version':'0.3.0'}}})
   account=await bridge.rpc('account/read',{})
   assert account['account']['email']=='first@example.test'
   thread=await bridge.rpc('thread/start',{'cwd':tmp,'model':'mock-model','approvalPolicy':'never','sandbox':'read-only'})
   tid=thread['thread']['id'];pid=bridge.process.pid
   async def turn(text):
    events.clear()
    await bridge.on_client({'id':text,'method':'turn/start','params':{'threadId':tid,'input':[{'type':'text','text':text}]}})
    for _ in range(300):
     await asyncio.sleep(.1)
     if any(e.get('method')=='turn/completed' and e['params']['turn']['status']=='completed' for e in events):return
     if reader.done():reader.result()
    print('EVENTS',json.dumps(events)[-6500:]);raise RuntimeError('turn did not complete')
   await turn('first turn')
   reserve=os.environ.get('XSWAP_TEST_RESERVE')=='1'
   if reserve:
    bridge.pool.reserve_reached=True
    await bridge.on_server({'method':'account/rateLimits/updated','params':limits(10)})
   await turn('second turn')
   print('RESULT',json.dumps({'sameServer':pid==bridge.process.pid,'thread':tid,'switchedTo':bridge.current,'calls':[(c['path'],c['account']) for c in calls],'completed':any(e.get('method')=='turn/completed' and e['params']['turn']['status']=='completed' for e in events)}))
   assert bridge.current=='second'
   if browser_fixture:
    await assert_browser_setup(home,browser_fixture)
    print('PASS: actual browser runtime setup before and after account switch')
   assert [c['account'] for c in calls if c['path']=='/v1/responses']==(['first','second'] if reserve else ['first','first','second'])
   assert not home.joinpath('auth.json').exists()
   data=await bridge.rpc('thread/read',{'threadId':tid,'includeTurns':True})
   assert len(data['thread']['turns'])==(2 if reserve else 3)
   print('PASS: same server PID and thread; '+('proactive 10% weekly switch before error, 2 turns' if reserve else 'quota error failover, 3 turns')+'; no auth.json')
  finally:
   reader.cancel();err.cancel()
   for t in list(bridge.tasks):t.cancel()
   await asyncio.gather(reader,err,*bridge.tasks,return_exceptions=True)
   if bridge.process.returncode is None:bridge.process.terminate()
   await bridge.process.wait()
 server.shutdown()
if __name__ == "__main__":
 asyncio.run(main())
