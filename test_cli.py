import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
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
 def test_prune_never_removes_running_dir(self):
  run_dir,fd=self.make_run('running',updated_at=time.time()-8*86400,hold_lock=True)
  try:
   with contextlib.redirect_stdout(io.StringIO()) as out:
    show_status(self.manager,prune=True)
   self.assertTrue(run_dir.exists())
   self.assertEqual(json.loads(out.getvalue())['pruned'],0)
  finally:
   os.close(fd)
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
