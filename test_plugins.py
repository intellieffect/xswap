import contextlib, io, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from codex_swap import Manager, SwapError
from xswap_plugins import ensure_plugins, repair

class PluginTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name);self.source=self.base/'source';self.home=self.base/'home'
  self.cache=self.source/'plugins/cache/browser/v1';self.cache.mkdir(parents=True)
  (self.cache/'client.mjs').write_text('original');self.home.mkdir()
 def legacy(self):
  (self.home/'plugins').symlink_to(self.source/'plugins',target_is_directory=True)
 def test_atomic_migration_localizes_realpath_and_preserves_source(self):
  self.legacy();(self.source/'plugins/cache/browser/latest').symlink_to('v1')
  (self.source/'plugins/.plugin-appserver').mkdir()
  (self.source/'plugins/.plugin-appserver/private-state').write_text('not cache')
  (self.source/'plugins/.plugin-appserver/codex').write_text('helper code')
  (self.home/'auth.json').write_text('do not touch');(self.home/'state_5.sqlite').write_text('session')
  self.assertEqual(ensure_plugins(self.home,self.source),'materialized')
  target=self.home/'plugins/cache/browser/v1/client.mjs'
  self.assertTrue(target.resolve().is_relative_to(self.home.resolve()))
  self.assertFalse((self.home/'plugins').is_symlink())
  self.assertFalse((self.home/'plugins/.plugin-appserver/private-state').exists())
  self.assertEqual((self.home/'plugins/.plugin-appserver/codex').read_text(),'helper code')
  target.write_text('local');self.assertEqual((self.cache/'client.mjs').read_text(),'original')
  self.assertEqual((self.home/'auth.json').read_text(),'do not touch')
  self.assertEqual((self.home/'state_5.sqlite').read_text(),'session')
  self.assertEqual(ensure_plugins(self.home,self.source),'already-local')
  self.assertEqual(target.read_text(),'local')
 def test_failed_exchange_preserves_original_link(self):
  self.legacy()
  with patch('xswap_plugins.exchange_paths',side_effect=OSError('fixture')):
   with self.assertRaises(OSError):ensure_plugins(self.home,self.source)
  self.assertTrue((self.home/'plugins').is_symlink())
  self.assertEqual((self.home/'plugins/cache/browser/v1/client.mjs').read_text(),'original')
 def test_escape_link_fails_closed(self):
  self.legacy();outside=self.base/'outside';outside.write_text('outside')
  (self.cache/'escape').symlink_to(outside)
  with self.assertRaises(SwapError):ensure_plugins(self.home,self.source)
  self.assertTrue((self.home/'plugins').is_symlink())
 def test_dry_run_has_no_mutation(self):
  self.legacy();before=set(self.home.iterdir())
  self.assertEqual(ensure_plugins(self.home,self.source,True),'would-materialize')
  self.assertEqual(set(self.home.iterdir()),before)
 def test_new_home_has_local_cache(self):
  ensure_plugins(self.home,self.source)
  self.assertTrue((self.home/'plugins/cache/browser/v1/client.mjs').resolve().is_relative_to(self.home.resolve()))
 def test_registered_profile_and_both_auto_homes_are_repaired(self):
  manager=Manager(self.base/'store',self.source)
  (self.source/'auth.json').write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"fixture"}')
  (self.source/'auth.json').chmod(0o600)
  manager.register('main');profile=manager.prepare('second')
  # Reproduce all v0.3.0 legacy homes; auto links chain through the profile.
  import shutil
  shutil.rmtree(profile/'plugins');(profile/'plugins').symlink_to(self.source/'plugins')
  homes=[manager.root/'auto/codex',manager.root/'auto/cli-codex',profile]
  for home in homes[:2]:home.mkdir(parents=True);(home/'plugins').symlink_to(profile/'plugins')
  with contextlib.redirect_stdout(io.StringIO()):repair(manager)
  for home in homes:self.assertTrue((home/'plugins/cache/browser/v1/client.mjs').resolve().is_relative_to(home.resolve()))

 def test_cycle_through_shared_source_is_rejected(self):
  alias=self.base/'alias';alias.mkdir();(alias/'plugins').symlink_to(self.source/'plugins')
  (self.cache/'loop').symlink_to(self.cache.parent)
  with self.assertRaises(SwapError):ensure_plugins(self.home,alias)
  self.assertFalse((self.home/'plugins').exists())
