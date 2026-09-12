import contextlib
import fcntl
import io
import json
import os
import time
from unittest import TestCase
from unittest.mock import patch
import test_codex_swap
from xswap_cli import enable, disable, interactive_args, server_overrides, read_settings, launch_cli, show_status, reconnect_wrapper

class CliTests(TestCase):
 setUp=test_codex_swap.AccountTests.setUp
 def test_routes_interactive_only(self):
  for args in ([],['hello'],['resume','--last'],['fork'],['-m','gpt-5','hello'],['--config','a=1','resume']):
   self.assertTrue(interactive_args(args),args)
  for args in (['exec','hi'],['-m','gpt-5','exec','hi'],['login'],['--version'],['--remote','unix:///tmp/a']):
   self.assertFalse(interactive_args(args),args)
 def test_server_configuration_forwarding(self):
  self.assertEqual(server_overrides(['-m','x','-c','a=1','--enable','foo','--config=b=2','--disable=bar','-C','/tmp','hello']),['-c','a=1','--enable','foo','--config=b=2','--disable=bar'])
 def setup_pool(self):
  self.manager.register('main');self.manager.prepare('second')
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
  logged,_=self.make_run('logged',lock=False,mtime=now-120);(logged/'bridge.log').write_text('fixture')  # any other file keeps the week
  with contextlib.redirect_stdout(io.StringIO()) as out:
   show_status(self.manager)
  self.assertFalse(empty.exists());self.assertFalse(lock_only.exists())
  self.assertTrue(fresh.exists());self.assertTrue(logged.exists())
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
  from xswap_doctor import check_auto_runs
  from xswap_upgrade import count_running_sessions
  stale,_=self.make_run('stale',updated_at=time.time()-8*86400)
  running,fd=self.make_run('running',updated_at=time.time()-8*86400,hold_lock=True)
  try:
   with patch.dict(os.environ,{'CODEX_SWAP_HOME':str(self.manager.root),'CODEX_HOME':str(self.source)}):
    self.assertEqual(count_running_sessions(),1)
   self.assertEqual(check_auto_runs(self.manager)['status'],'OK')
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
