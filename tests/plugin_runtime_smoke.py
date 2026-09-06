"""Opt-in: actual shipped trusted runtime, browser setup only (no browser actions).
Fake credentials are unnecessary: no account login, network request, or page access.
"""
import asyncio, contextlib, json, os, signal, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xswap_plugins import ensure_plugins

BUNDLE = Path('/Applications/ChatGPT.app/Contents/Resources/cua_node')
async def bootstrap(home, relative, imported_client, repair_source=None):
 env={'PATH':os.environ['PATH'],'HOME':str(Path.home()),'TMPDIR':tempfile.gettempdir(),
      'CODEX_HOME':str(home),'NODE_REPL_NODE_PATH':str(BUNDLE/'bin/node'),
      'NODE_REPL_NODE_MODULE_DIRS':str(BUNDLE/'lib/node_modules'),
      'NODE_REPL_TRUSTED_CODE_PATHS':str(home)+':'+str(BUNDLE/'lib/node_modules'),
      'NODE_REPL_TRUSTED_SERVICES':json.dumps({'browser':str(home/'plugins/cache'/relative/'scripts/browser-service.mjs')})}
 process=await asyncio.create_subprocess_exec(str(BUNDLE/'bin/node_repl'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env,start_new_session=True)
 counter=0
 async def rpc(method,params):
  nonlocal counter
  counter+=1;key=counter
  process.stdin.write((json.dumps({'jsonrpc':'2.0','id':key,'method':method,'params':params})+'\n').encode());await process.stdin.drain()
  while line:=await asyncio.wait_for(process.stdout.readline(),30):
   data=json.loads(line)
   if data.get('id')==key:return data
  raise AssertionError('runtime exited')
 try:
  await rpc('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'xswap-regression','version':'0.3.1'}})
  process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n');await process.stdin.drain()
  code='const { setupBrowserRuntime } = await import('+json.dumps(str(imported_client))+'); const agent = await setupBrowserRuntime(); console.log("XSWAP_BROWSER_SETUP_OK");'
  result=await rpc('tools/call',{'name':'js','arguments':{'code':code,'title':'Plugin trusted path regression'}})
  if repair_source is not None:
   assert 'Trusted RPC dependency must resolve within a configured trusted code path' in json.dumps(result),result
   ensure_plugins(home,repair_source)
   result=await rpc('tools/call',{'name':'js','arguments':{'code':code,'title':'Retry after live plugin repair'}})
   assert 'Trusted RPC dependency must resolve within a configured trusted code path' in json.dumps(result),result
   reset=await rpc('tools/call',{'name':'js_reset','arguments':{}})
   assert not reset.get('error'),reset
   result=await rpc('tools/call',{'name':'js','arguments':{'code':code,'title':'Initialize after supported tool reset'}})
   assert process.returncode is None
  return json.dumps(result)
 finally:
  with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGTERM)
  try:await asyncio.wait_for(process.wait(),5)
  except asyncio.TimeoutError:process.kill();await process.wait()
  await asyncio.sleep(.2)
  with contextlib.suppress(ProcessLookupError):os.killpg(process.pid,signal.SIGKILL)

def plugin_fixture(home):
 source=Path(os.environ.get('XSWAP_PLUGIN_SOURCE',Path.home()/'.codex'))
 versions=sorted((source/'plugins/cache/openai-bundled/browser').glob('*/scripts/browser-client.mjs'))
 assert versions,'browser plugin missing'
 client=versions[-1];relative=client.parent.parent.relative_to(source/'plugins/cache')
 ensure_plugins(home,source)
 return relative,client

async def assert_browser_setup(home, fixture):
 result=await bootstrap(home,*fixture)
 assert 'XSWAP_BROWSER_SETUP_OK' in result and '\"isError\": true' not in result,result

async def main():
 source=Path(os.environ.get('XSWAP_PLUGIN_SOURCE',Path.home()/'.codex'))
 versions=sorted((source/'plugins/cache/openai-bundled/browser').glob('*/scripts/browser-client.mjs'))
 assert versions,'browser plugin missing'
 client=versions[-1];relative=client.parent.parent.relative_to(source/'plugins/cache')
 with tempfile.TemporaryDirectory(prefix='xswap-trust-') as tmp:
  # Same browser/client code and trust policy, only legacy link vs local copy changes.
  home=Path(tmp)/'codex';home.mkdir();(home/'plugins').symlink_to(source/'plugins',target_is_directory=True)
  before=await bootstrap(home,relative,client)
  assert 'Trusted RPC dependency must resolve within a configured trusted code path' in before,before
  recovered=await bootstrap(home,relative,client,repair_source=source)
  assert 'XSWAP_BROWSER_SETUP_OK' in recovered,recovered
  for imported in (client,home/'plugins/cache'/relative/'scripts/browser-client.mjs'):
   after=await bootstrap(home,relative,imported)
   assert 'XSWAP_BROWSER_SETUP_OK' in after and '"isError": true' not in after,after
  assert not (home/'auth.json').exists()
  print('PASS: actual runtime rejects shared symlink; canonical and profile imports initialize after atomic cache materialization; cached failure recovers after js_reset with same MCP supervisor; trust roots unchanged; no auth copied')

if __name__=='__main__':asyncio.run(main())
