import contextlib, io, json, tempfile, unittest
from pathlib import Path
import test_live
from test_live import limits
from xswap_live import quota_available, validate_threshold, LiveError, AccountPool
from xswap_cli import set_policy
from codex_swap import Manager

class ReserveTests(unittest.TestCase):
 def test_boundary_and_weekly_duration_not_window_position(self):
  self.assertFalse(quota_available(limits(10),weekly_remaining=10))
  self.assertTrue(quota_available(limits(10.1),weekly_remaining=10))
  raw=limits(1);raw['rateLimits']['primary']['windowDurationMins']=300
  raw['rateLimits']['secondary']=limits(50)['rateLimits']['primary']
  self.assertTrue(quota_available(raw,weekly_remaining=10))
  raw['rateLimits']['secondary']['usedPercent']=90
  self.assertFalse(quota_available(raw,weekly_remaining=10))
 def test_unknown_weekly_does_not_qualify(self):
  raw=limits();raw['rateLimits']['primary']['windowDurationMins']=300
  self.assertIsNone(quota_available(raw,weekly_remaining=10))
  self.assertTrue(quota_available(raw,weekly_remaining=0))
 def test_validation(self):
  for value in [-1,100,101,float('nan'),float('inf'),True,'10']:
   with self.assertRaises(LiveError):validate_threshold(value)
  self.assertEqual(validate_threshold(10.5),10.5)
 def test_setting_preserves_pool_wrapper_and_reloads_existing_pool(self):
  with tempfile.TemporaryDirectory() as tmp:
   manager=Manager(Path(tmp)/'store',Path(tmp)/'source')
   settings={'enabled':True,'accounts':['a','b'],'wrapper':{'path':'fixture'}}
   (manager.root/'auto.json').write_text(json.dumps(settings))
   pool=object.__new__(AccountPool);pool.manager=manager
   self.assertEqual(pool.weekly_remaining,0)
   with contextlib.redirect_stdout(io.StringIO()):set_policy(manager,10)
   self.assertEqual(pool.weekly_remaining,10)
   saved=json.loads((manager.root/'auto.json').read_text())
   for key,value in settings.items():self.assertEqual(saved[key],value)
   with self.assertRaises(LiveError):set_policy(manager,float('nan'))
   self.assertEqual(pool.weekly_remaining,10)

class ReserveBridgeTests(unittest.IsolatedAsyncioTestCase):
 asyncSetUp=test_live.BridgeTests.asyncSetUp
 asyncTearDown=test_live.BridgeTests.asyncTearDown
 async def test_switch_before_turn_without_failed_turn_or_restart(self):
  self.pool.weekly_remaining=10;self.bridge.last_quota=limits(10)
  await self.bridge.on_client({'id':1,'method':'turn/start','params':{'threadId':'same','input':[{'type':'text','text':'work'}]}})
  self.assertEqual(self.bridge.current,'second')
  self.assertEqual([c[0] for c in self.calls],['account/login/start'])
  self.assertEqual(self.sent[0]['params']['threadId'],'same')
 async def test_below_threshold_candidates_do_not_ping_pong(self):
  self.pool.weekly_remaining=10;self.bridge.last_quota=limits(9)
  self.pool.quota['second']=limits(10)
  await self.bridge.before_turn()
  self.assertEqual(self.bridge.current,'first');self.assertFalse(self.calls)
 async def test_active_turn_is_not_interrupted(self):
  self.pool.weekly_remaining=10;self.bridge.last_quota=limits(10)
  self.bridge.active.add('working')
  await self.bridge.before_turn()
  self.assertFalse(self.calls)
  self.bridge.active.clear();await self.bridge.before_turn()
  self.assertEqual(self.bridge.current,'second')
 async def test_threshold_changes_apply_to_existing_bridge(self):
  self.bridge.last_quota=limits(15);self.pool.weekly_remaining=10
  await self.bridge.before_turn();self.assertEqual(self.bridge.current,'first')
  self.pool.weekly_remaining=20
  await self.bridge.before_turn();self.assertEqual(self.bridge.current,'second')
