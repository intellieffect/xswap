import asyncio
import contextlib
import fcntl
import io
import json
import os
import time
from unittest import TestCase
from unittest.mock import patch
import test_codex_swap
import test_live
from xswap_cli import enable, disable, interactive_args, server_overrides, read_settings, launch_cli, show_status, reconnect_wrapper, codex_main, passthrough_subcommand, serve_cli
from xswap_live import LiveError

class CliTests(TestCase):
 setUp=test_codex_swap.AccountTests.setUp
 def test_routes_interactive_only(self):
  for args in ([],['hello'],['resume','--last'],['fork'],['-m','gpt-5','hello'],['--config','a=1','resume']):
   self.assertTrue(interactive_args(args),args)
  for args in (['exec','hi'],['-m','gpt-5','exec','hi'],['login'],['--version'],['--remote','unix:///tmp/a'],['upgrade']):
   self.assertFalse(interactive_args(args),args)
 def test_server_configuration_forwarding(self):
  self.assertEqual(server_overrides(['-m','x','-c','a=1','--enable','foo','--config=b=2','--disable=bar','-C','/tmp','hello']),['-c','a=1','--enable','foo','--config=b=2','--disable=bar'])
 def setup_pool(self):
  self.manager.register('main');self.manager.prepare('second')
  # Wrapper checks walk PATH; never let a fixture see (or re-point) this machine's real codex entries.
  self.enterContext(patch.dict(os.environ,{'PATH':str(self.base)}))
 def test_wrapper_roundtrip_and_real_executable(self):
  self.setup_pool()
  real=self.base/'real-codex';real.write_text('fixture');real.chmod(0o700)
  proxy=self.base/'xswap-codex';proxy.write_text('fixture');proxy.chmod(0o700)
  cli=self.base/'codex';cli.symlink_to(real)
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()):
   enable(self.manager,'main,second',wrap=True)
   self.assertEqual(cli.resolve(),proxy)
   self.assertEqual(self.manager.codex(),str(real))
   enable(self.manager,'main,second',wrap=True)
   self.assertEqual(read_settings(self.manager)['wrapper']['originalTarget'],str(real))
   self.assertNotIn('wrappers',read_settings(self.manager))  # re-wrapping the same entry adds no second record
   disable(self.manager)
   self.assertEqual(cli.resolve(),real)
   self.assertFalse(read_settings(self.manager)['enabled'])
  self.assertEqual(len(list(self.manager.root.rglob('auth.json'))),0)
 def wrapped_fixture(self):
  # A user-owned codex symlink wrapped by xswap, plus the release directory a Codex update would install.
  self.setup_pool()
  real=self.base/'real-codex';real.write_text('fixture');real.chmod(0o700)
  updated=self.base/'packages'/'standalone'/'releases'/'0.155.0'/'bin';updated.mkdir(parents=True)
  updated=updated/'codex';updated.write_text('fixture');updated.chmod(0o700)
  proxy=self.base/'xswap-codex';proxy.write_text('fixture');proxy.chmod(0o700)
  cli=self.base/'codex';cli.symlink_to(real)
  return real,updated,proxy,cli
 def test_codex_update_that_replaced_the_link_is_reconnected_on_next_use(self):
  # Codex's standalone updater (ctrl+u, `codex upgrade`, install.sh) re-points ~/.local/bin/codex at its
  # new release, so plain `codex` silently bypassed xswap until someone re-ran auto-enable --wrap-codex.
  real,updated,proxy,cli=self.wrapped_fixture()
  from codex_swap import plain_codex_notice
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
   enable(self.manager,'main,second',wrap=True)
   cli.unlink();cli.symlink_to(updated)  # what the updater does
   self.assertEqual(self.manager.codex(),str(updated))  # any launch / usage read reconnects
   self.assertEqual(cli.resolve(),proxy)
   wrapper=read_settings(self.manager)['wrapper']
   self.assertEqual(wrapper['realCodex'],str(updated));self.assertEqual(wrapper['originalTarget'],str(updated))
   self.assertIn('reconnected',err.getvalue())
   err.truncate(0);err.seek(0)
   self.assertEqual(self.manager.codex(),str(updated));self.assertEqual(err.getvalue(),'')  # quiet once connected
   cli.unlink();cli.symlink_to(updated)
   self.assertIsNone(plain_codex_notice(self.manager,str(self.base/'elsewhere')))  # use/switch reconnect too
   self.assertEqual(cli.resolve(),proxy)
   disable(self.manager)
   self.assertEqual(cli.resolve(),updated)  # rollback lands on the updated release, not the stale one
 def test_reconnect_leaves_dangling_foreign_or_disabled_links_alone(self):
  real,updated,proxy,cli=self.wrapped_fixture()
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   enable(self.manager,'main,second',wrap=True)
   cli.unlink();cli.symlink_to(self.base/'missing')
   self.assertIsNone(reconnect_wrapper(self.manager));self.assertEqual(os.readlink(cli),str(self.base/'missing'))
   plain=self.base/'notes.txt';plain.write_text('fixture');cli.unlink();cli.symlink_to(plain)
   self.assertIsNone(reconnect_wrapper(self.manager));self.assertEqual(os.readlink(cli),str(plain))
   cli.unlink();cli.symlink_to(proxy)
   disable(self.manager)
   cli.unlink();cli.symlink_to(updated)  # executable, but automatic switching is off
   self.assertIsNone(reconnect_wrapper(self.manager));self.assertEqual(os.readlink(cli),str(updated))
   self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'],str(real))
 def drift(self):
  from xswap_cli import wrapper_drift
  return wrapper_drift(read_settings(self.manager))
 def test_wrapper_drift_names_every_skip_reason_and_reconnect_stays_silent(self):
  # Every condition that used to be a bare None now carries a classified reason; reconnect_wrapper
  # still returns None, leaves the link alone, and prints nothing for each of them (INT-5186 item 10).
  from xswap_cli import DRIFT_REASONS, status_data
  real,updated,proxy,cli=self.wrapped_fixture()
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
   enable(self.manager,'main,second',wrap=True)
   self.assertEqual(self.drift(),{'action':'skip','reason':'ok','path':str(cli),'target':str(proxy),'real':str(proxy),'proxy':str(proxy),'error':None})
   state=status_data(self.manager,cleanup=False)
   self.assertEqual((state['codexWrapped'],state['wrapperReason']),(True,'ok'))
   seen={}
   cli.unlink();cli.symlink_to(self.base/'missing')
   seen['dangling-target']=self.drift()
   plain=self.base/'notes.txt';plain.write_text('fixture');cli.unlink();cli.symlink_to(plain)
   seen['not-executable']=self.drift()
   cli.unlink();cli.write_text('fixture')  # a reinstall dropped a regular file where the link was
   seen['not-a-symlink']=self.drift()
   cli.unlink()
   seen['missing']=self.drift()
   cli.symlink_to(updated)
   with patch('os.getuid',return_value=os.getuid()+1):
    seen['not-user-owned']=self.drift()
   with patch('os.readlink',side_effect=PermissionError(13,'Permission denied')):
    seen['unreadable']=self.drift()
   for reason,drift in seen.items():
    self.assertEqual((drift['action'],drift['reason'],drift['path'],drift['proxy']),('skip',reason,str(cli),str(proxy)),reason)
    self.assertIn(reason,DRIFT_REASONS)
   self.assertEqual(seen['dangling-target']['target'],str(self.base/'missing'))
   self.assertEqual(seen['dangling-target']['real'],str(self.base/'missing'))
   self.assertEqual(seen['not-executable']['target'],str(plain))
   self.assertIsNone(seen['missing']['target'])
   self.assertEqual(seen['not-user-owned']['target'],str(updated))
   self.assertEqual(seen['unreadable']['error'],'Permission denied')
   # The only reconnect case: user-owned link at another executable while switching is enabled.
   drift=self.drift()
   self.assertEqual((drift['action'],drift['reason'],drift['target'],drift['real']),('reconnect','replaced',str(updated),str(updated)))
   # Silent skips: re-create each skip state and confirm reconnect_wrapper neither writes nor prints.
   cli.unlink();cli.symlink_to(self.base/'missing')
   self.assertIsNone(reconnect_wrapper(self.manager));self.assertEqual(os.readlink(cli),str(self.base/'missing'))
   state=status_data(self.manager,cleanup=False)
   self.assertEqual((state['codexWrapped'],state['wrapperReason']),(False,'dangling-target'))
   cli.unlink();cli.write_text('fixture')
   self.assertIsNone(reconnect_wrapper(self.manager));self.assertTrue(cli.is_file() and not cli.is_symlink())
   cli.unlink()
   self.assertIsNone(reconnect_wrapper(self.manager));self.assertFalse(os.path.lexists(cli))
   self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'],str(real))
   self.assertEqual(err.getvalue(),'')
   cli.symlink_to(proxy)
   disable(self.manager)
   cli.unlink();cli.symlink_to(updated)  # executable, but automatic switching is off
   drift=self.drift()
   self.assertEqual((drift['action'],drift['reason'],drift['target']),('skip','auto-disabled',str(updated)))
   self.assertIsNone(reconnect_wrapper(self.manager));self.assertEqual(os.readlink(cli),str(updated))
   self.assertEqual(err.getvalue(),'')
 def test_wrapper_drift_without_a_recorded_wrapper(self):
  from xswap_cli import wrapper_drift
  empty={'action':'skip','reason':'not-wrapped','path':None,'target':None,'real':None,'proxy':None,'error':None}
  self.assertEqual(wrapper_drift({}),empty)
  self.assertEqual(wrapper_drift({'enabled':True,'wrapper':{}}),empty)
  self.assertEqual(wrapper_drift({'wrapper':{'path':'/x/codex'}}),{**empty,'path':'/x/codex'})  # no proxy recorded
 def test_describe_drift_pairs_cause_and_fix_without_secrets(self):
  from xswap_cli import DRIFT_REASONS, describe_drift, wrapper_drift
  self.assertEqual(describe_drift(wrapper_drift({}),'CMD'),('',''))
  self.assertEqual(describe_drift({'reason':'ok','path':'/x','target':'/p','real':'/p','proxy':'/p','error':None},'CMD'),('',''))
  drift={'action':'skip','reason':'dangling-target','path':'/x/codex','target':'/x/gone','real':'/x/gone','proxy':'/x/xswap-codex','error':None}
  self.assertEqual(describe_drift(drift,'xswap auto-enable --accounts a,b --wrap-codex'),
   ('/x/codex -> /x/gone does not exist, so xswap leaves it alone','reinstall Codex or point the link at a Codex executable, then run: xswap auto-enable --accounts a,b --wrap-codex'))
  for reason in DRIFT_REASONS:
   cause,fix=describe_drift({**drift,'reason':reason,'error':'Permission denied'},'CMD')
   if reason in ('ok','not-wrapped'):
    self.assertEqual((cause,fix),('',''),reason)
   else:
    self.assertTrue(cause and fix.endswith('CMD'),reason)
    self.assertNotIn('reconnects it',cause+fix,reason)  # skips never promise an automatic reconnect
 def test_bridged_session_end_reconnects_after_in_session_update(self):
  # ctrl+u runs the updater inside the bridged TUI; the link must be back before the next plain `codex`.
  real,updated,proxy,cli=self.wrapped_fixture()
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  async def fake_serve(pool,real_path,args,env,status_path,socket_path):
   cli.unlink();cli.symlink_to(updated)
   return 0
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
   enable(self.manager,'main,second',wrap=True)
   with patch('xswap_cli.serve_cli',fake_serve),patch('xswap_plugins.ensure_plugins'):
    self.assertEqual(launch_cli(self.manager,'main,second',[]),0)
  self.assertEqual(cli.resolve(),proxy)
  self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'],str(updated))
  self.assertIn('reconnected',err.getvalue())
 def test_disable_preserves_external_change(self):
  self.setup_pool()
  real=self.base/'real';real.touch();proxy=self.base/'proxy';proxy.touch();cli=self.base/'codex';cli.symlink_to(real)
  with patch('shutil.which',side_effect=lambda n:str(proxy if n=='xswap-codex' else cli)),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   enable(self.manager,'main,second',wrap=True)
   cli.unlink();cli.symlink_to('external-update')
   disable(self.manager)
   self.assertEqual(os.readlink(cli),'external-update')
 def test_codex_main_reports_missing_real_codex_explicitly(self):
  self.setup_pool()
  gone=self.base/'gone'/'codex'
  from codex_swap import atomic_json
  atomic_json(self.manager.root/'auto.json',{'enabled':True,'accounts':['main','second'],'wrapper':{'path':str(self.base/'codex'),'originalTarget':str(gone),'realCodex':str(gone),'proxy':str(self.base/'xswap-codex')}})
  from xswap_cli import codex_main
  calls=[]
  with patch.dict(os.environ,{'CODEX_SWAP_HOME':str(self.manager.root),'CODEX_HOME':str(self.source)}),patch('xswap_cli.sys.argv',['xswap-codex','--version']),patch('xswap_cli.os.execve',side_effect=lambda *a:calls.append(a)),contextlib.redirect_stderr(io.StringIO()) as err:
   self.assertEqual(codex_main(),1)
  self.assertEqual(calls,[])
  text=err.getvalue()
  self.assertIn('real Codex executable is unavailable',text);self.assertIn(str(gone),text)
  self.assertIn('xswap auto-enable --accounts main,second --wrap-codex',text)
 def test_codex_main_passes_through_when_real_codex_exists(self):
  self.setup_pool()
  real=self.base/'real-codex';real.write_text('fixture');real.chmod(0o700)
  from codex_swap import atomic_json
  atomic_json(self.manager.root/'auto.json',{'enabled':True,'accounts':['main','second'],'wrapper':{'path':str(self.base/'codex'),'originalTarget':str(real),'realCodex':str(real),'proxy':str(self.base/'xswap-codex')}})
  from xswap_cli import codex_main
  calls=[]
  def fake_execve(path,argv,env):
   calls.append((path,argv));raise OSError('stop')
  with patch.dict(os.environ,{'CODEX_SWAP_HOME':str(self.manager.root),'CODEX_HOME':str(self.source)}),patch('xswap_cli.sys.argv',['xswap-codex','--version']),patch('xswap_cli.os.execve',side_effect=fake_execve),contextlib.redirect_stderr(io.StringIO()):
   codex_main()
  self.assertEqual(calls,[(str(real),[str(real),'--version'])])
 def test_manager_codex_names_recovery_when_real_is_missing(self):
  real,updated,proxy,cli=self.wrapped_fixture()
  from codex_swap import SwapError
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()):
   enable(self.manager,'main,second',wrap=True)
   real.unlink()  # what purging the directory that held the release does
   with self.assertRaisesRegex(SwapError,'xswap auto-enable --accounts main,second --wrap-codex') as caught:
    self.manager.codex()
  self.assertIn(str(real),str(caught.exception))
 def test_disable_reports_a_missing_original_target(self):
  real,updated,proxy,cli=self.wrapped_fixture()
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()):
   enable(self.manager,'main,second',wrap=True)
  real.unlink()
  with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
   disable(self.manager)
  self.assertEqual(os.readlink(cli),str(real))  # restored as asked, and said so
  self.assertIn(str(real),err.getvalue());self.assertIn('is missing',err.getvalue())
  self.assertFalse(read_settings(self.manager)['enabled'])
 def test_disable_names_relocate_when_restored_target_is_inside_root(self):
  self.setup_pool()
  inside=self.manager.root/'auto'/'cli-codex'/'packages'/'standalone'/'current'/'bin'/'codex'
  inside.parent.mkdir(parents=True);inside.write_text('fixture');inside.chmod(0o700)
  proxy=self.base/'xswap-codex';proxy.write_text('fixture');proxy.chmod(0o700)
  cli=self.base/'codex';cli.symlink_to(proxy)
  from codex_swap import atomic_json
  atomic_json(self.manager.root/'auto.json',{'enabled':True,'accounts':['main','second'],'wrapper':{'path':str(cli),'originalTarget':str(inside),'realCodex':str(inside),'proxy':str(proxy)}})
  with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
   disable(self.manager)
  self.assertEqual(os.readlink(cli),str(inside))
  self.assertIn('run: xswap relocate-codex',err.getvalue())
 def test_enable_warns_when_the_wrapped_target_is_inside_root(self):
  self.setup_pool()
  inside=self.manager.root/'auto'/'cli-codex'/'packages'/'standalone'/'current'/'bin'/'codex'
  inside.parent.mkdir(parents=True);inside.write_text('fixture');inside.chmod(0o700)
  proxy=self.base/'xswap-codex';proxy.write_text('fixture');proxy.chmod(0o700)
  cli=self.base/'codex';cli.symlink_to(inside)
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
   enable(self.manager,'main,second',wrap=True)
  self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'],str(inside))
  self.assertIn('run: xswap relocate-codex',err.getvalue())
 def shadow_fixture(self):
  # bin-b/codex is the entry xswap wrapped (originally -> a brew release); a Codex standalone
  # install then puts bin-a/codex -> standalone ahead of it on PATH (2026-09-10).
  self.setup_pool()
  self.bin_a=self.base/'bin-a';self.bin_b=self.base/'bin-b';self.bin_a.mkdir();self.bin_b.mkdir()
  self.brew=self.base/'brew-codex';self.brew.write_text('fixture');self.brew.chmod(0o700)
  self.standalone=self.base/'standalone'/'bin'/'codex';self.standalone.parent.mkdir(parents=True);self.standalone.write_text('fixture');self.standalone.chmod(0o700)
  self.proxy=self.base/'xswap-codex';self.proxy.write_text('fixture');self.proxy.chmod(0o700)
  (self.bin_b/'codex').symlink_to(self.brew)
  self.first=self.bin_b/'codex'  # what shutil.which('codex') returns; moved to bin-a below
  self.enterContext(patch('shutil.which',side_effect=lambda n:str(self.proxy if n=='xswap-codex' else self.first)))
  self.enterContext(patch.dict(os.environ,{'PATH':os.pathsep.join([str(self.bin_a),str(self.bin_b)])}))
  self.enterContext(contextlib.redirect_stdout(io.StringIO()))
  self.err=self.enterContext(contextlib.redirect_stderr(io.StringIO()))
  enable(self.manager,'main,second',wrap=True)
  self.assertEqual(os.readlink(self.bin_b/'codex'),str(self.proxy))
  (self.bin_a/'codex').symlink_to(self.standalone);self.first=self.bin_a/'codex'
 def test_standalone_install_ahead_of_wrapped_entry_is_wrapped_on_next_use(self):
  self.shadow_fixture()
  self.assertEqual(self.manager.codex(),str(self.standalone))  # any launch / usage read wraps it
  self.assertEqual(os.readlink(self.bin_a/'codex'),str(self.proxy));self.assertEqual(os.readlink(self.bin_b/'codex'),str(self.proxy))
  settings=read_settings(self.manager)
  self.assertEqual(settings['wrapper'],{'path':str(self.bin_a/'codex'),'originalTarget':str(self.standalone),'realCodex':str(self.standalone),'proxy':str(self.proxy)})
  self.assertEqual(settings['wrappers'],[{'path':str(self.bin_b/'codex'),'originalTarget':str(self.brew),'realCodex':str(self.brew),'proxy':str(self.proxy)}])
  text=self.err.getvalue()
  self.assertIn(f'{self.bin_a/"codex"} had appeared ahead of {self.bin_b/"codex"} on PATH',text);self.assertIn('connected it to xswap-codex',text)
  self.err.truncate(0);self.err.seek(0)
  self.assertEqual(self.manager.codex(),str(self.standalone));self.assertEqual(self.err.getvalue(),'')  # quiet once connected
  disable(self.manager)
  self.assertEqual(os.readlink(self.bin_a/'codex'),str(self.standalone));self.assertEqual(os.readlink(self.bin_b/'codex'),str(self.brew))
  settings=read_settings(self.manager)
  self.assertNotIn('wrappers',settings);self.assertEqual(settings['wrapper']['path'],str(self.bin_a/'codex'))
 def test_shadowing_entry_is_left_alone_when_the_wrapped_entry_is_not_on_path(self):
  self.shadow_fixture()
  with patch.dict(os.environ,{'PATH':str(self.bin_a)}):
   self.assertIsNone(reconnect_wrapper(self.manager))
  self.assertEqual(os.readlink(self.bin_a/'codex'),str(self.standalone))
  self.assertEqual(read_settings(self.manager)['wrapper']['path'],str(self.bin_b/'codex'))
  self.assertEqual(self.err.getvalue(),'')
 def test_shadowing_regular_file_is_left_alone(self):
  self.shadow_fixture()
  (self.bin_a/'codex').unlink();(self.bin_a/'codex').write_text('fixture');(self.bin_a/'codex').chmod(0o700)
  self.assertIsNone(reconnect_wrapper(self.manager))
  self.assertFalse((self.bin_a/'codex').is_symlink())
  self.assertEqual(read_settings(self.manager)['wrapper']['path'],str(self.bin_b/'codex'))
 def test_shadowing_entry_is_ignored_while_auto_is_disabled(self):
  self.shadow_fixture()
  disable(self.manager)
  self.assertIsNone(reconnect_wrapper(self.manager))
  self.assertEqual(os.readlink(self.bin_a/'codex'),str(self.standalone))
 def test_enable_wrap_keeps_the_previously_wrapped_entry_for_rollback(self):
  self.shadow_fixture()
  with patch.dict(os.environ,{'PATH':str(self.bin_a)}):  # off PATH: nothing self-heals, the user runs the command doctor names
   enable(self.manager,'main,second',wrap=True)
  self.assertEqual(os.readlink(self.bin_a/'codex'),str(self.proxy))
  settings=read_settings(self.manager)
  self.assertEqual(settings['wrapper']['path'],str(self.bin_a/'codex'));self.assertEqual(settings['wrapper']['realCodex'],str(self.standalone))
  self.assertEqual([w['path'] for w in settings['wrappers']],[str(self.bin_b/'codex')])
  enable(self.manager,'main,second',wrap=True)  # idempotent: no duplicate records
  self.assertEqual(len(read_settings(self.manager)['wrappers']),1)
  disable(self.manager)
  self.assertEqual(os.readlink(self.bin_a/'codex'),str(self.standalone));self.assertEqual(os.readlink(self.bin_b/'codex'),str(self.brew))
 def test_disable_restores_every_wrapped_entry_and_reports_external_changes(self):
  self.shadow_fixture()
  self.manager.codex()
  (self.bin_b/'codex').unlink();(self.bin_b/'codex').symlink_to('external-update')
  disable(self.manager)
  self.assertEqual(os.readlink(self.bin_a/'codex'),str(self.standalone))
  self.assertEqual(os.readlink(self.bin_b/'codex'),'external-update')
  self.assertIn(f'codex entry {self.bin_b/"codex"} changed outside xswap; left it untouched.',self.err.getvalue())
 def test_auto_status_codex_wrapped_follows_the_path_lookup(self):
  from xswap_cli import status_data
  self.shadow_fixture()
  self.assertFalse(status_data(self.manager,cleanup=False)['codexWrapped'])
  self.manager.codex()
  self.assertTrue(status_data(self.manager,cleanup=False)['codexWrapped'])
 def test_codex_path_entries_orders_classifies_and_dedupes(self):
  from xswap_cli import codex_path_entries
  self.shadow_fixture()
  alias=self.base/'alias';alias.mkdir();(alias/'codex').symlink_to(os.path.relpath(self.proxy,alias))
  dangling=self.base/'dangling';dangling.mkdir();(dangling/'codex').symlink_to(self.base/'missing')
  folder=self.base/'folder';folder.mkdir();(folder/'codex').mkdir()
  with patch.dict(os.environ,{'PATH':os.pathsep.join(['',str(dangling),str(folder),str(self.bin_a),str(alias),str(self.bin_b),str(self.bin_a)])}):
   entries=codex_path_entries(read_settings(self.manager))
  self.assertEqual([(e['path'],e['kind'],e['target']) for e in entries],[
   (str(self.bin_a/'codex'),'foreign',str(self.standalone)),
   (str(alias/'codex'),'wrapper',os.path.relpath(self.proxy,alias)),
   (str(self.bin_b/'codex'),'wrapper',str(self.proxy))])
 def test_new_run_dir_makes_every_level_private_and_repairs_auto(self):
  # A CLI launch used to create `auto` through mkdir(parents=True), i.e. with the umask (INT-5085).
  import os,stat
  from xswap_cli import new_run_dir
  old=os.umask(0o022);self.addCleanup(os.umask,old)
  auto=self.manager.root/'auto';auto.mkdir(parents=True);auto.chmod(0o755)
  run_dir=new_run_dir(self.manager)
  for path in (auto,auto/'cli-runs',run_dir):
   self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o700,path)
  self.assertEqual(run_dir.parent,auto/'cli-runs')
 def test_dry_run_does_not_create_runtime(self):
  self.setup_pool()
  with patch.object(self.manager,'codex',return_value='/fixture/codex'),contextlib.redirect_stdout(io.StringIO()):
   launch_cli(self.manager,'main,second',[],dry=True)
  self.assertFalse((self.manager.root/'auto').exists())
 def make_run(self,name,updated_at=None,hold_lock=False,event=None,reason=None,lock=True,mtime=None):
  # A bridge record as serve_cli leaves it: status.json (whitelisted fields only) and the
  # lock file the bridge flocks while alive. lock=False mimics a TUI that exited before
  # its bridge ever connected; mtime backdates the directory for records without status.
  run_dir=self.manager.root/'auto'/'cli-runs'/name;run_dir.mkdir(parents=True)
  if updated_at is not None:
   state={'updatedAt':updated_at}
   if event is not None:
    state['event']=event
   if reason is not None:
    state['reason']=reason
   (run_dir/'status.json').write_text(json.dumps(state))
  fd=None
  if lock:
   fd=os.open(run_dir/'.bridge.lock',os.O_CREAT|os.O_RDWR,0o600)
   if hold_lock:
    fcntl.flock(fd,fcntl.LOCK_EX)
   else:
    os.close(fd);fd=None
  if mtime is not None:
   os.utime(run_dir,(mtime,mtime))
  return run_dir,fd
 def test_status_exposes_stop_reason_and_quota_flag(self):
  run_dir,_=self.make_run('failed-init')
  (run_dir/'status.json').write_text(json.dumps({'updatedAt':time.time(),'event':'stopped','reason':'usage request timed out','quotaKnown':False,'accessToken':'must-not-leak'}))
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  session=[s for s in json.loads(out.getvalue())['sessions'] if s['event']=='stopped'][0]
  self.assertEqual(session['reason'],'usage request timed out')
  self.assertIs(session['quotaKnown'],False)
  self.assertNotIn('accessToken',session)
 def test_status_exposes_manual_reason_last_failure_and_log_path(self):
  # 2026-09-10: three records ended manualState "failed" with reason null and nothing pointed at a log.
  run_dir,_=self.make_run('failed-switch')
  failure={'event':'manual-switch-failed','account':'main','candidate':'second','reason':'usage service unavailable','at':time.time()}
  (run_dir/'status.json').write_text(json.dumps({'updatedAt':time.time(),'event':'stopped','account':'main','manualState':'failed','manualReason':'usage service unavailable','lastFailure':failure}))
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  session=[s for s in json.loads(out.getvalue())['sessions'] if s['manualState']=='failed'][0]
  self.assertEqual(session['manualReason'],'usage service unavailable')
  self.assertEqual(session['lastFailure'],failure)
  self.assertIsNone(session['log'])
  (run_dir/'bridge.log').write_text('fixture\n')
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  session=[s for s in json.loads(out.getvalue())['sessions'] if s['manualState']=='failed'][0]
  self.assertEqual(session['log'],str(run_dir/'bridge.log'))
 def test_status_exposes_the_server_confirmed_login(self):
  # `account` is the bridge's own claim; only the verified* fields came from its app server.
  run_dir,fd=self.make_run('verified',hold_lock=True)
  try:
   (run_dir/'status.json').write_text(json.dumps({'updatedAt':time.time(),'event':'ready','account':'main','verifiedAccount':'main','verifiedIdentity':'main@example.test','verifiedAt':1700000000.0,'verifyReason':None}))
   with contextlib.redirect_stdout(io.StringIO()) as out:
    show_status(self.manager)
   session=[s for s in json.loads(out.getvalue())['sessions'] if s['running']][0]
   self.assertEqual((session['verifiedAccount'],session['verifiedIdentity'],session['verifiedAt'],session['verifyReason']),('main','main@example.test',1700000000.0,None))
  finally:
   os.close(fd)
 def test_prune_never_removes_running_dir(self):
  run_dir,fd=self.make_run('running',updated_at=time.time()-8*86400,hold_lock=True)
  try:
   with contextlib.redirect_stdout(io.StringIO()) as out:
    show_status(self.manager,prune=True)
   self.assertTrue(run_dir.exists())
   self.assertEqual(json.loads(out.getvalue())['pruned'],0)
  finally:
   os.close(fd)
 def test_cleanup_false_keeps_stale_dir(self):
  # The contract `xswap upgrade` relies on for its running-session count.
  from xswap_cli import status_data
  run_dir,_=self.make_run('stale-menu',updated_at=time.time()-8*86400)
  result=status_data(self.manager,cleanup=False)
  self.assertTrue(run_dir.exists())
  self.assertEqual(result['pruned'],0)
  self.assertEqual(result['prunedRuns'],[])
 def test_clean_stop_goes_after_a_day_failures_and_vanished_bridges_keep_a_week(self):
  now=time.time()
  gone,_=self.make_run('stopped-old',updated_at=now-25*3600,event='stopped')
  kept,_=self.make_run('stopped-new',updated_at=now-23*3600,event='stopped')
  failed,_=self.make_run('stopped-failed',updated_at=now-25*3600,event='stopped',reason='app-server exited')
  killed,_=self.make_run('killed',updated_at=now-25*3600,event='policy-applied')  # bridge died without its stopped write
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  self.assertFalse(gone.exists())
  for path in (kept,failed,killed):
   self.assertTrue(path.exists(),path)
  report=json.loads(out.getvalue())
  self.assertEqual(report['pruned'],1)
  self.assertEqual([(r['run'],r['rule'],r['event'],r['reason']) for r in report['prunedRuns']],[('stopped-old','stopped','stopped',None)])
  self.assertEqual(sorted((s['event'],s['reason'] or '') for s in report['sessions']),
                   [('policy-applied',''),('stopped',''),('stopped','app-server exited')])
 def test_empty_record_goes_after_a_minute_but_a_fresh_one_survives(self):
  now=time.time()
  empty,_=self.make_run('empty-old',lock=False,mtime=now-120)
  lock_only,_=self.make_run('lock-only',mtime=now-120)  # took its lock, died before the first status write
  fresh,_=self.make_run('empty-fresh',lock=False)
  # Backdate after writing, not through make_run: creating a file resets the directory mtime,
  # and a record with no status.json has nothing else to date it — written the other way round
  # these two come out 0 s old and survive on the 60 s floor without ever reaching run_dir_empty.
  logged,_=self.make_run('logged',lock=False);(logged/'bridge.log').write_text('fixture');os.utime(logged,(now-120,now-120))  # any other file keeps the week
  corrupt,_=self.make_run('corrupt',lock=False);(corrupt/'status.json').write_text('{not json');os.utime(corrupt,(now-120,now-120))  # an unreadable status is content too
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  self.assertFalse(empty.exists());self.assertFalse(lock_only.exists())
  self.assertTrue(fresh.exists());self.assertTrue(logged.exists());self.assertTrue(corrupt.exists())
  report=json.loads(out.getvalue())
  self.assertEqual(report['pruned'],2)
  self.assertEqual(sorted((r['run'],r['rule'],r['account'],r['event']) for r in report['prunedRuns']),
                   [('empty-old','empty',None,None),('lock-only','empty',None,None)])
  self.assertTrue(all(100<=r['ageSeconds']<=200 for r in report['prunedRuns']))
 def test_launch_sweeps_stale_records_before_creating_its_own(self):
  import stat
  from xswap_cli import new_run_dir
  now=time.time()
  stale,_=self.make_run('stale',updated_at=now-8*86400,event='policy-applied')
  stopped,_=self.make_run('stopped',updated_at=now-25*3600,event='stopped')
  empty,_=self.make_run('empty',lock=False,mtime=now-120)
  fresh,_=self.make_run('fresh',lock=False)
  running,fd=self.make_run('running',updated_at=now-8*86400,hold_lock=True)
  (self.manager.root/'auto.json').write_text('{not json')  # a launch must not depend on readable settings
  try:
   run_dir=new_run_dir(self.manager)
  finally:
   os.close(fd)
  for path in (stale,stopped,empty):
   self.assertFalse(path.exists(),path)
  for path in (fresh,running,run_dir):
   self.assertTrue(path.is_dir(),path)
  self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode),0o700)
 def test_list_text_prunes_but_json_stays_read_only(self):
  from codex_swap import main
  self.manager.register('main')
  stale,_=self.make_run('stale',updated_at=time.time()-8*86400)
  running,fd=self.make_run('running',updated_at=time.time()-8*86400,hold_lock=True)
  try:
   with patch('codex_swap.Manager',return_value=self.manager),contextlib.redirect_stdout(io.StringIO()):
    self.assertEqual(main(['list','--offline','--json']),0)
    self.assertTrue(stale.exists())  # completion runs this on every tab; no side effects
    self.assertEqual(main(['list','--offline']),0)
  finally:
   os.close(fd)
  self.assertFalse(stale.exists())
  self.assertTrue(running.exists())
 def test_dashboard_prunes_stale_records(self):
  from xswap_display import dashboard
  stale,_=self.make_run('stale',updated_at=time.time()-8*86400)
  data=dashboard(self.manager)
  self.assertFalse(stale.exists())
  self.assertEqual(data['sessions'],[])
  self.assertNotIn('prunedRuns',data)  # the menu app's payload is unchanged
 def test_upgrade_count_and_doctor_leave_records_alone(self):
  from codex_swap import __version__
  from xswap_doctor import check_auto_runs
  from xswap_upgrade import running_session_hints
  stale,_=self.make_run('stale',updated_at=time.time()-8*86400)
  running,fd=self.make_run('running',updated_at=time.time()-8*86400,hold_lock=True)
  try:
   with patch.dict(os.environ,{'CODEX_SWAP_HOME':str(self.manager.root),'CODEX_HOME':str(self.source)}):
    # Both records predate bridgeVersion, so the one holding its lock is reported.
    self.assertEqual(len(running_session_hints(__version__)),1)
   self.assertEqual(check_auto_runs(self.manager)['status'],'WARN')
  finally:
   os.close(fd)
  self.assertTrue(stale.exists());self.assertTrue(running.exists())
 def test_auto_status_reports_pruned_records_without_secrets(self):
  run_dir,_=self.make_run('stale')
  (run_dir/'status.json').write_text(json.dumps({'updatedAt':time.time()-8*86400,'event':'policy-applied','account':'main',
   'bridgeVersion':'0.7.6','accessToken':'must-not-leak'}))
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  self.assertNotIn('must-not-leak',out.getvalue())
  report=json.loads(out.getvalue())
  self.assertEqual(report['pruned'],1)
  record=report['prunedRuns'][0]
  self.assertEqual({k:record[k] for k in ('run','rule','account','event','bridgeVersion','reason')},
                   {'run':'stale','rule':'stale','account':'main','event':'policy-applied','bridgeVersion':'0.7.6','reason':None})
  self.assertTrue(8*86400-5<=record['ageSeconds']<=8*86400+5)
  self.assertEqual(set(record),{'run','rule','ageSeconds','account','event','bridgeVersion','reason'})
 def test_prune_flag_reports_forced_rule(self):
  self.make_run('fresh',updated_at=time.time()-5*60,event='ready')
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager,prune=True)
  self.assertEqual([(r['run'],r['rule']) for r in json.loads(out.getvalue())['prunedRuns']],[('fresh','forced')])
 def test_scan_survives_record_removed_by_another_sweep(self):
  # Sweeps now run from list, the menu bar, and every launch; a record can vanish between
  # the directory listing and opening its lock file.
  stale,_=self.make_run('stale',updated_at=time.time()-8*86400)
  with patch('xswap_cli.os.open',side_effect=FileNotFoundError),contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  report=json.loads(out.getvalue())
  self.assertEqual(report['pruned'],0)
  self.assertEqual(report['sessions'],[])
 def test_prune_removes_stale_dir_by_default(self):
  run_dir,_=self.make_run('stale',updated_at=time.time()-8*86400)
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  self.assertFalse(run_dir.exists())
  self.assertEqual(json.loads(out.getvalue())['pruned'],1)
 def test_prune_flag_removes_fresh_non_running_dir(self):
  run_dir,_=self.make_run('fresh',updated_at=time.time()-5*60)
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  self.assertTrue(run_dir.exists())
  self.assertEqual(json.loads(out.getvalue())['pruned'],0)
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager,prune=True)
  self.assertFalse(run_dir.exists())
  self.assertEqual(json.loads(out.getvalue())['pruned'],1)
 def test_prune_never_removes_booting_run(self):
  run_dir=self.manager.root/'auto'/'cli-runs'/'booting';run_dir.mkdir(parents=True)
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager,prune=True)
  self.assertTrue(run_dir.exists())
  self.assertEqual(json.loads(out.getvalue())['pruned'],0)
 def test_prune_survives_rmtree_errors(self):
  run_dir,_=self.make_run('raceremoved',updated_at=time.time()-8*86400)
  with patch('xswap_cli.shutil.rmtree',side_effect=FileNotFoundError),contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  self.assertTrue(run_dir.exists())
  self.assertEqual(json.loads(out.getvalue())['pruned'],0)
 def test_prune_never_follows_symlink(self):
  target=self.base/'external-run';target.mkdir()
  (target/'status.json').write_text(json.dumps({'updatedAt':time.time()-30*86400}))
  runs=self.manager.root/'auto'/'cli-runs';runs.mkdir(parents=True,exist_ok=True)
  link=runs/'linked';link.symlink_to(target,target_is_directory=True)
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager,prune=True)
  self.assertTrue(target.exists())
  self.assertTrue(link.is_symlink())
  self.assertEqual(json.loads(out.getvalue())['pruned'],0)
 THREAD='00000000-0000-4000-8000-000000000001'
 def bridged_run(self,name,state,hold_lock=True):
  # A run record as a bridge writes it, its .bridge.lock held while "running".
  from codex_swap import atomic_json
  run_dir,fd=self.make_run(name,hold_lock=hold_lock)
  if fd is not None: self.addCleanup(os.close,fd)
  atomic_json(run_dir/'status.json',{'account':'main','updatedAt':time.time(),**state})
  return run_dir
 def save_thread(self,home,thread=THREAD):
  path=home/'sessions'/'2026'/'09'/'13';path.mkdir(parents=True,exist_ok=True)
  (path/f'rollout-2026-09-13T00-00-00-{thread}.jsonl').write_text('{}\n')
 def test_status_exposes_conversation_home_and_pool(self):
  runtime=self.manager.root/'auto'/'cli-codex'
  self.bridged_run('x',{'conversationId':self.THREAD,'codexHome':str(runtime),'accounts':['main','second'],'accessToken':'must-not-leak'},hold_lock=False)
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  [session]=json.loads(out.getvalue())['sessions']
  self.assertEqual((session['conversationId'],session['codexHome'],session['accounts']),(self.THREAD,str(runtime),['main','second']))
  self.assertNotIn('must-not-leak',out.getvalue())
 def test_bridge_hints_flag_only_running_sessions_on_another_version(self):
  from codex_swap import __version__,atomic_json
  from xswap_cli import bridge_hints,status_data
  runtime=self.manager.root/'auto'/'cli-codex';self.save_thread(runtime)
  atomic_json(self.manager.root/'auto.json',{'enabled':True,'accounts':['main','second']})
  self.bridged_run('a-old',{'bridgeVersion':'0.7.2','conversationId':self.THREAD,'codexHome':str(runtime)})
  self.bridged_run('b-current',{'bridgeVersion':__version__,'conversationId':self.THREAD,'codexHome':str(runtime)})
  self.bridged_run('c-stopped',{'bridgeVersion':'0.7.2','event':'stopped','conversationId':self.THREAD,'codexHome':str(runtime)},hold_lock=False)
  self.bridged_run('d-legacy',{})  # pre-0.7.6 record: no bridgeVersion, no conversation
  hints=bridge_hints(self.manager,status_data(self.manager,cleanup=False),__version__)
  self.assertEqual([(h['surface'],h['account'],h['bridgeVersion']) for h in hints],[('cli','main','0.7.2'),('cli','main',None)])
  self.assertEqual(hints[0]['hint'],f'bridge 0.7.2 · reopen with xswap run -- resume {self.THREAD} to load {__version__}')
  self.assertEqual(hints[1]['hint'],f'bridge unknown (older than 0.7.6) · exit and reopen it to load {__version__}')
 def test_bridge_hint_uses_plain_codex_when_the_wrapper_is_connected(self):
  from codex_swap import __version__,atomic_json
  from xswap_cli import bridge_hints,status_data
  # codexWrapped follows the PATH lookup (item 1), so the entry must be a real
  # executable on the pinned PATH or the enumeration skips it.
  self.setup_pool()
  runtime=self.manager.root/'auto'/'cli-codex';self.save_thread(runtime)
  proxy=self.base/'xswap-codex';proxy.write_text('fixture');proxy.chmod(0o700)
  cli=self.base/'codex';cli.symlink_to(proxy)
  atomic_json(self.manager.root/'auto.json',{'enabled':True,'accounts':['main','second'],'wrapper':{'path':str(cli),'proxy':str(proxy),'originalTarget':str(proxy),'realCodex':str(proxy)}})
  self.bridged_run('old',{'bridgeVersion':'0.7.2','conversationId':self.THREAD,'codexHome':str(runtime)})
  state=status_data(self.manager,cleanup=False)
  self.assertTrue(state['codexWrapped'])
  [hint]=bridge_hints(self.manager,state,__version__)
  self.assertEqual(hint['hint'],f'bridge 0.7.2 · reopen with codex resume {self.THREAD} to load {__version__}')
 def test_bridge_hint_names_the_pool_when_auto_defaults_are_off(self):
  from codex_swap import __version__
  from xswap_cli import bridge_hints,status_data
  runtime=self.manager.root/'auto'/'cli-codex';self.save_thread(runtime)
  self.bridged_run('old',{'bridgeVersion':'0.7.2','conversationId':self.THREAD,'codexHome':str(runtime),'account':'second','accounts':['main','second']})
  [hint]=bridge_hints(self.manager,status_data(self.manager,cleanup=False),__version__)
  self.assertEqual(hint['hint'],f'bridge 0.7.2 · reopen with xswap run --auto --accounts second,main -- resume {self.THREAD} to load {__version__}')
  self.assertNotIn('CODEX_HOME',hint['hint'])
 def test_bridge_hint_never_names_an_unsaved_conversation_and_desktop_reopens_the_app(self):
  from codex_swap import __version__,atomic_json
  from xswap_cli import bridge_hints,status_data
  self.bridged_run('old',{'bridgeVersion':'0.7.2','conversationId':self.THREAD,'codexHome':str(self.manager.root/'auto'/'cli-codex')})
  desktop=self.manager.root/'auto';atomic_json(desktop/'status.json',{'account':'main','bridgeVersion':'0.7.2','updatedAt':time.time()})
  fd=os.open(desktop/'.bridge.lock',os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX);self.addCleanup(os.close,fd)
  hints=bridge_hints(self.manager,status_data(self.manager,cleanup=False),__version__)
  self.assertEqual([h['hint'] for h in hints],[f'bridge 0.7.2 · quit and reopen it with xswap app to load {__version__}',f'bridge 0.7.2 · exit and reopen it to load {__version__}'])
 def test_bridge_hint_needs_both_a_rollout_file_and_a_pool_of_two_to_name_a_command(self):
  from codex_swap import __version__,atomic_json
  from xswap_cli import bridge_hints,status_data
  # Two independent rules send a session to the generic line, and the fixture above
  # satisfies neither, so each needs a record where only the other rule could fire:
  # a thread with no rollout file cannot be resumed at all, and with auto defaults
  # off the record must name its own pool (a pre-0.8.0 record names none).
  runtime=self.manager.root/'auto'/'cli-codex';auto=self.manager.root/'auto.json'
  generic=f'bridge 0.7.2 · exit and reopen it to load {__version__}'
  atomic_json(auto,{'enabled':True,'accounts':['main','second']})
  self.bridged_run('old',{'bridgeVersion':'0.7.2','conversationId':self.THREAD,'codexHome':str(runtime)})
  [unsaved]=bridge_hints(self.manager,status_data(self.manager,cleanup=False),__version__)
  self.assertEqual(unsaved['hint'],generic)  # auto would name it; the thread has no rollout file
  self.save_thread(runtime)
  [named]=bridge_hints(self.manager,status_data(self.manager,cleanup=False),__version__)
  self.assertEqual(named['hint'],f'bridge 0.7.2 · reopen with xswap run -- resume {self.THREAD} to load {__version__}')
  atomic_json(auto,{'enabled':False,'accounts':['main','second']})
  [lone]=bridge_hints(self.manager,status_data(self.manager,cleanup=False),__version__)
  self.assertEqual(lone['hint'],generic)  # saved now, but one account is not a pool to reopen with
 def test_list_shows_the_reopen_hint_under_the_running_session(self):
  from codex_swap import __version__,atomic_json
  self.manager.register('main')
  runtime=self.manager.root/'auto'/'cli-codex';self.save_thread(runtime)
  atomic_json(self.manager.root/'auto.json',{'enabled':True,'accounts':['main','second']})
  self.bridged_run('old',{'bridgeVersion':'0.7.2','conversationId':self.THREAD,'codexHome':str(runtime)})
  with contextlib.redirect_stdout(io.StringIO()) as out:
   self.manager.show_accounts(offline=True)
  lines=out.getvalue().splitlines()
  group=lines.index('  ● CLI · main · 1 session')
  self.assertEqual(lines[group+1],f'    ⚠ bridge 0.7.2 · reopen with xswap run -- resume {self.THREAD} to load {__version__}')
  self.assertNotIn('\033',out.getvalue())

class PassthroughTests(TestCase):
 # `codex exec` and the other non-interactive subcommands reach the real Codex through
 # os.execve; the wrapper must hand them the xswap-selected account's home the way
 # `xswap run -- exec` does, or say why it could not (audit item 3, INT-5186).
 setUp=CliTests.setUp
 setup_pool=CliTests.setup_pool
 wrapped_fixture=CliTests.wrapped_fixture
 def wrap(self):
  # Connected codex command: `main` (self.source, signed in) is selected, `second` is prepared but not signed in.
  real,updated,proxy,cli=self.wrapped_fixture()
  def which(name):return str(proxy if name=='xswap-codex' else cli)
  self.which=which
  with patch('shutil.which',side_effect=which),contextlib.redirect_stdout(io.StringIO()):
   enable(self.manager,'main,second',wrap=True)
  return real
 def run_wrapper(self,args,extra_env=None):
  # Invoke the wrapped codex entry with os.execve captured; returns (path, argv, env, stderr).
  calls=[]
  environ={'OPENAI_API_KEY':'do-not-inherit','CODEX_API_KEY':'do-not-inherit',**(extra_env or {})}
  with patch('codex_swap.Manager',return_value=self.manager),patch.dict(os.environ,environ),patch('sys.argv',['xswap-codex',*args]),patch('xswap_cli.os.execve',side_effect=lambda path,argv,env:calls.append((path,argv,env))),contextlib.redirect_stderr(io.StringIO()) as err:
   for key in ('CODEX_HOME','XSWAP_BYPASS','XSWAP_QUIET'):
    if key not in environ:os.environ.pop(key,None)
   self.assertIsNone(codex_main())
  self.assertEqual(len(calls),1,args)
  path,argv,env=calls[0]
  return path,argv,env,err.getvalue()
 def signed_in_second(self):
  from codex_swap import atomic_json
  second=self.manager.account('second')[1];atomic_json(second/'auth.json',self.auth)
  return second
 def mapped_project(self,name):
  project=self.base/'project';project.mkdir(exist_ok=True)
  self.manager.map_dir(name,str(project))
  self.addCleanup(os.chdir,os.getcwd());os.chdir(project)
  return project
 def test_passthrough_subcommand_skips_option_values_and_excludes_install_commands(self):
  for args,expected in ((['exec','hi'],'exec'),(['e','hi'],'e'),(['-c','a=1','-m','gpt-5','review'],'review'),(['--config=a=1','mcp','list'],'mcp'),(['mcp-server'],'mcp-server'),(['doctor'],'doctor'),(['login'],None),(['logout'],None),(['app'],None),(['app-server','--stdio'],None),(['completion','zsh'],None),(['help'],None),(['update'],None),(['upgrade'],None),(['--version'],None),(['exec','-h'],None),(['--remote=unix:///tmp/a'],None),([],None)):
   self.assertEqual(passthrough_subcommand(args),expected,args)
 def test_exec_runs_as_the_selected_account_without_inherited_secrets(self):
  real=self.wrap()
  path,argv,env,err=self.run_wrapper(['exec','hi'])
  self.assertEqual((path,argv),(str(real),[str(real),'exec','hi']))
  self.assertEqual(env['CODEX_HOME'],str(self.source))
  for key in ('OPENAI_API_KEY','CODEX_API_KEY'):
   self.assertNotIn(key,env)
  self.assertEqual(err,'xswap: running codex exec as main\n')
  self.assertNotIn('fake-token',err)
 def test_directory_mapping_wins_over_the_selection(self):
  self.wrap()
  second=self.signed_in_second()
  project=self.mapped_project('second')
  _,argv,env,err=self.run_wrapper(['-m','gpt-5','review'])
  self.assertEqual(argv[1:],['-m','gpt-5','review'])
  self.assertEqual(env['CODEX_HOME'],str(second))
  self.assertEqual(err,f'xswap: running codex review as second (mapped by {project})\n')
 def test_excluded_commands_and_flags_keep_the_callers_home_silently(self):
  self.wrap()
  for args in (['login'],['logout','--all'],['app'],['app-server','--stdio'],['completion','zsh'],['help','exec'],['update'],['upgrade'],['--version'],['exec','--help'],['-h'],['--remote','unix:///tmp/a','exec','hi']):
   _,argv,env,err=self.run_wrapper(args)
   self.assertEqual(argv[1:],args)
   self.assertNotIn('CODEX_HOME',env,args)
   self.assertEqual(env['OPENAI_API_KEY'],'do-not-inherit',args)
   self.assertEqual(err,'',args)
 def test_explicit_codex_home_and_bypass_are_respected(self):
  self.wrap()
  elsewhere=str(self.base/'elsewhere')
  _,_,env,err=self.run_wrapper(['exec','hi'],{'CODEX_HOME':elsewhere})
  self.assertEqual(env['CODEX_HOME'],elsewhere);self.assertEqual(env['OPENAI_API_KEY'],'do-not-inherit');self.assertEqual(err,'')
  _,_,env,err=self.run_wrapper(['exec','hi'],{'XSWAP_BYPASS':'1'})
  self.assertNotIn('CODEX_HOME',env);self.assertEqual(env['OPENAI_API_KEY'],'do-not-inherit');self.assertEqual(err,'')
 def test_disabled_or_unsigned_selection_falls_back_with_a_warning(self):
  self.wrap()
  self.manager.set_disabled('main',True)
  _,_,env,err=self.run_wrapper(['exec','hi'])
  self.assertNotIn('CODEX_HOME',env);self.assertEqual(env['OPENAI_API_KEY'],'do-not-inherit')
  self.assertEqual(err,f'xswap: codex exec is running with its own home {self.manager.source}, not the xswap selection (account main is disabled; run: xswap enable main)\n')
  self.manager.set_disabled('main',False)
  self.mapped_project('second')  # prepared, never signed in
  _,_,env,err=self.run_wrapper(['exec','hi'])
  self.assertNotIn('CODEX_HOME',env)
  self.assertEqual(err,f'xswap: codex exec is running with its own home {self.manager.source}, not the xswap selection (account second is not signed in; run: xswap login second)\n')
 def test_rejected_login_falls_back_with_the_login_command(self):
  # A login the usage service rejected (auth-state.json) must not run `codex exec` against
  # a dead account: the wrapper keeps the caller's own home and names the one fix.
  from codex_swap import identity
  self.wrap()
  self.manager.remember_auth_failure('main',identity(self.source))
  _,_,env,err=self.run_wrapper(['exec','hi'])
  self.assertNotIn('CODEX_HOME',env);self.assertEqual(env['OPENAI_API_KEY'],'do-not-inherit')
  self.assertEqual(err,f'xswap: codex exec is running with its own home {self.manager.source}, not the xswap selection (account main needs a new login: the usage service rejected it; run: xswap login main)\n')
  # `xswap login main` clears the record; the next pass-through runs as the account again.
  with self.manager.locked():self.manager._forget_auth_failure('main')
  _,_,env,err=self.run_wrapper(['exec','hi'])
  self.assertEqual(env['CODEX_HOME'],str(self.source));self.assertEqual(err,'xswap: running codex exec as main\n')
 def test_no_selected_account_falls_back_with_a_warning(self):
  from codex_swap import atomic_json
  self.wrap()
  data=self.manager.read();data['active']=None;atomic_json(self.manager.registry,data)
  _,_,env,err=self.run_wrapper(['exec','hi'])
  self.assertNotIn('CODEX_HOME',env);self.assertEqual(env['OPENAI_API_KEY'],'do-not-inherit')
  self.assertEqual(err,f'xswap: codex exec is running with its own home {self.manager.source}, not the xswap selection (no account selected; run: xswap use NAME)\n')
 def test_keyring_store_falls_back_with_the_store_reason(self):
  self.wrap()
  second=self.signed_in_second()
  (second/'config.toml').unlink();(second/'config.toml').write_text('cli_auth_credentials_store = "keyring"\n')
  self.mapped_project('second')
  _,_,env,err=self.run_wrapper(['exec','hi'])
  self.assertNotIn('CODEX_HOME',env)
  self.assertTrue(err.startswith(f'xswap: codex exec is running with its own home {self.manager.source}, not the xswap selection ('),err)
  self.assertIn("credential storage is 'keyring'",err);self.assertNotIn('fake-token',err)
 def test_quiet_env_silences_the_notice_but_not_the_warning(self):
  self.wrap()
  _,_,env,err=self.run_wrapper(['exec','hi'],{'XSWAP_QUIET':'1'})
  self.assertEqual(env['CODEX_HOME'],str(self.source));self.assertNotIn('OPENAI_API_KEY',env);self.assertEqual(err,'')
  self.manager.set_disabled('main',True)
  _,_,env,err=self.run_wrapper(['exec','hi'],{'XSWAP_QUIET':'1'})
  self.assertNotIn('CODEX_HOME',env);self.assertIn('account main is disabled; run: xswap enable main',err)
 def test_disabled_auto_mode_leaves_pass_through_untouched(self):
  self.wrap()
  with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   disable(self.manager)
  # xswap-codex reached by name, or through a link disable() could not restore, stays transparent.
  _,_,env,err=self.run_wrapper(['exec','hi'])
  self.assertNotIn('CODEX_HOME',env);self.assertEqual(env['OPENAI_API_KEY'],'do-not-inherit');self.assertEqual(err,'')
 def test_interactive_invocations_still_take_the_bridge(self):
  # Guard: the pass-through branch must not swallow the interactive path.
  self.wrap()
  with patch('codex_swap.Manager',return_value=self.manager),patch.dict(os.environ,{}),patch('sys.argv',['xswap-codex','hello']),patch('xswap_cli.launch_cli',return_value=0) as bridge,patch('xswap_cli.os.execve') as execve:
   os.environ.pop('CODEX_HOME',None);os.environ.pop('XSWAP_BYPASS',None)
   self.assertEqual(codex_main(),0)
  bridge.assert_called_once_with(self.manager,'main,second',['hello'])
  execve.assert_not_called()
 def test_run_upgrade_launches_into_the_profile_home_instead_of_the_bridge(self):
  # `upgrade` was in neither subcommand set, so `xswap run -- upgrade` was treated as
  # interactive and installed the release with CODEX_HOME=auto/cli-codex. It now launches
  # directly; the profile's `packages` link is what keeps the release out of xswap's state.
  real=self.wrap()
  second=self.signed_in_second()
  self.mapped_project('second')
  calls=[]
  with patch('shutil.which',side_effect=self.which),patch('xswap_cli.launch_cli') as bridge,patch('codex_swap.subprocess.call',side_effect=lambda command,env:calls.append((command,env)) or 0):
   self.assertEqual(self.manager.launch_cli(None,['upgrade']),0)
  bridge.assert_not_called()
  command,env=calls[0]
  self.assertEqual(command,[str(real),'upgrade'])
  self.assertEqual(env['CODEX_HOME'],str(second))
  self.assertEqual(os.readlink(second/'packages'),str(self.source/'packages'))

class PassthroughDocsTests(TestCase):
 # Classifying `upgrade` as non-interactive changed `xswap run -- upgrade` too: it leaves the
 # bridge and installs through the profile's `packages` link. A user asking where the release
 # lands reads the pass-through paragraph, not the CHANGELOG, so that paragraph has to say it.
 cases=(('README.md','Once connected, non-interactive commands follow the selection too.',
         ('`xswap run -- upgrade`','`packages` link','reference Codex home')),
        ('README.ko.md','xswap: running codex exec as work',
         ('`xswap run -- upgrade`','`packages` 링크','기준 Codex 홈')))
 def test_both_readmes_say_where_run_upgrade_installs_the_release(self):
  from pathlib import Path
  for name,marker,required in self.cases:
   blocks=[block for block in (Path(__file__).parent/name).read_text().split('\n\n') if marker in block]
   self.assertEqual(len(blocks),1,name)  # the anchor moved or was duplicated; re-find the paragraph
   for phrase in required:
    self.assertIn(phrase,blocks[0],name)

class DisconnectNoticeTests(TestCase):
 """After the TUI exits, serve_cli names bridge.log when the bridge failed or recorded a failure."""
 setUp=test_codex_swap.AccountTests.setUp
 def serve(self,run):
  # Drives serve_cli's real handler and exit path with a fake listener, CLI process, and bridge class.
  run_dir=self.manager.root/'auto'/'cli-runs'/'notice';run_dir.mkdir(parents=True)
  socket_path=self.base/'rpc.sock';socket_path.touch()
  finished=None
  class FakeSocket:
   def __aiter__(self):return self
   async def __anext__(self):raise StopAsyncIteration
   async def send(self,text):pass
  class FakeServe:
   def __init__(self,handle,*args,**kwargs):self.handle=handle
   async def __aenter__(self):
    self.task=asyncio.create_task(self.handle(FakeSocket()));return self
   async def __aexit__(self,*exc):await self.task
   def close(self):pass
   async def wait_closed(self):pass
  class FakeBridge:
   def __init__(self,pool,argv,env,socket=None,status_path=None):
    self.current='first';self.failure=None;self.last_failure=None;self.resume_thread=None
   async def run(self):
    try:
     await run(self)
    finally:
     finished.set()
  class FakeProcess:
   pid=4242;returncode=0
   async def wait(self):
    await finished.wait();return 0
  async def spawn(*args,**kwargs):return FakeProcess()
  async def main():
   nonlocal finished
   finished=asyncio.Event()
   return await serve_cli(test_live.Pool(),'/fixture/codex',[],{'CODEX_HOME':str(self.base/'home')},run_dir/'status.json',socket_path,bridge_class=FakeBridge)
  with patch('websockets.asyncio.server.unix_serve',FakeServe),patch('xswap_cli.asyncio.create_subprocess_exec',spawn),contextlib.redirect_stderr(io.StringIO()) as err:
   code=asyncio.run(main())
  return code,err.getvalue(),run_dir
 def test_bridge_failure_names_the_log(self):
  async def run(bridge):
   bridge.failure='app-server exited';raise LiveError('app-server exited')
  code,err,run_dir=self.serve(run)
  self.assertEqual(code,0)
  self.assertIn(f'xswap auto: CLI bridge disconnected (app-server exited); see {run_dir/"bridge.log"} or xswap auto-status.',err)
 def test_failure_in_a_healthy_session_is_named_at_exit(self):
  async def run(bridge):
   bridge.last_failure={'event':'manual-switch-failed','account':'first','candidate':'second','reason':'usage service unavailable','at':0}
  code,err,run_dir=self.serve(run)
  self.assertEqual(code,0)
  self.assertIn(f'xswap auto: last failure in this session: manual-switch-failed (usage service unavailable); see {run_dir/"bridge.log"}.',err)
  self.assertNotIn('disconnected',err)
 def test_clean_session_prints_nothing(self):
  async def run(bridge):pass
  code,err,_=self.serve(run)
  self.assertEqual((code,err),(0,''))

class ResumeHomeTests(TestCase):
 session_id='00000000-0000-4000-8000-000000000001'
 def setUp(self):
  test_codex_swap.AccountTests.setUp(self)
  home_patch=patch('xswap_cli.Path.home',return_value=self.base/'user')
  home_patch.start();self.addCleanup(home_patch.stop)
 def saved(self,home):
  path=home/'sessions'/'2026'/'09'/'06'
  path.mkdir(parents=True,exist_ok=True)
  (path/f'rollout-2026-09-06T00-00-00-{self.session_id}.jsonl').write_text('{}\n')
 def test_explicit_resume_uses_original_home_without_copying(self):
  from xswap_cli import resume_home
  self.saved(self.source)
  runtime=self.manager.root/'auto'/'cli-codex'
  self.assertEqual(resume_home(self.manager,['resume',self.session_id],runtime),self.source)
  self.assertFalse(runtime.exists())
 def test_runtime_copy_takes_precedence(self):
  from xswap_cli import resume_home
  runtime=self.manager.root/'auto'/'cli-codex'
  self.saved(self.source);self.saved(runtime)
  self.assertEqual(resume_home(self.manager,['-m','example','resume','--all',self.session_id],runtime),runtime)
 def test_picker_last_and_prompts_keep_runtime(self):
  from xswap_cli import resume_home
  self.saved(self.source)
  runtime=self.manager.root/'auto'/'cli-codex'
  for args in ([],['resume'],['resume','--last'],['hello',self.session_id],['resume','named-session']):
   self.assertEqual(resume_home(self.manager,args,runtime),runtime)
 def test_registered_profile_fork(self):
  from xswap_cli import resume_home
  home=self.manager.prepare('other');self.saved(home)
  runtime=self.manager.root/'auto'/'cli-codex'
  self.assertEqual(resume_home(self.manager,['fork',self.session_id],runtime),home)

class GlobalSelectionTests(TestCase):
 setUp = CliTests.setUp
 setup_pool = CliTests.setup_pool
 def test_use_updates_new_session_pool_and_preserves_settings(self):
  from codex_swap import atomic_json
  self.setup_pool()
  atomic_json(self.manager.root/'auto.json', {'enabled':True, 'accounts':['second'], 'weeklyRemainingThreshold':10})
  self.manager.use('main')
  settings=read_settings(self.manager)
  self.assertEqual(settings['accounts'], ['main','second'])
  self.assertEqual(settings['weeklyRemainingThreshold'],10)
  self.manager.use('main')
  self.assertEqual(read_settings(self.manager)['accounts'], ['main','second'])
