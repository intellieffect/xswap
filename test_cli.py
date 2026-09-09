import contextlib
import fcntl
import io
import json
import os
import time
from unittest import TestCase
from unittest.mock import patch
import test_codex_swap
from xswap_cli import enable, disable, interactive_args, server_overrides, read_settings, launch_cli, show_status

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
 def make_run(self,name,updated_at=None,hold_lock=False):
  run_dir=self.manager.root/'auto'/'cli-runs'/name;run_dir.mkdir(parents=True)
  if updated_at is not None:
   (run_dir/'status.json').write_text(json.dumps({'updatedAt':updated_at}))
  fd=os.open(run_dir/'.bridge.lock',os.O_CREAT|os.O_RDWR,0o600)
  if hold_lock:
   fcntl.flock(fd,fcntl.LOCK_EX)
  else:
   os.close(fd);fd=None
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
 def test_menu_status_does_not_prune(self):
  from xswap_cli import status_data
  run_dir,_=self.make_run('stale-menu',updated_at=time.time()-8*86400)
  result=status_data(self.manager,cleanup=False)
  self.assertTrue(run_dir.exists())
  self.assertEqual(result['pruned'],0)
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
