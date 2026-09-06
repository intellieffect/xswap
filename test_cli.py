import contextlib
import io
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import test_codex_swap
from xswap_cli import enable, disable, interactive_args, server_overrides, read_settings, launch_cli

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
