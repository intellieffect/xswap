import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { sync } from '../xswap_bridge/openclaw.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'xswap-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const codexHome = path.join(root, 'codex'); fs.mkdirSync(codexHome);
  const expires = Date.now() + 3600_000;
  const auth = { auth_mode: 'chatgpt', tokens: { access_token: 'fake-access', refresh_token: 'fake-refresh', account_id: 'fake-account' } };
  const writeAuth = () => fs.writeFileSync(path.join(codexHome, 'auth.json'), JSON.stringify(auth), { mode: 0o600 }); writeAuth();
  let shared = { profiles: { 'openai:old': { provider: 'openai', type: 'oauth', access: 'old-token' } } };
  const local = { main: { order: { openai: ['openai:old'], anthropic: ['keep-me'] } }, worker: {} };
  let writes = 0;
  const request = { account: 'main', codexHome, backupRoot: path.join(root, 'backups') };
  const sdk = {
    resolveOpenAICodexAuthIdentity: ({ accountId }) => ({ accountId: accountId || 'fake-account', email: 'example@example.invalid' }),
    decodeOpenAICodexJwtPayload: () => ({ sub: 'fake-person' }),
    resolveOpenAICodexAccessTokenExpiry: () => expires, // overridden per-token in pool tests
    async updateAuthProfileStoreWithLock(params) {
      const snapshot = structuredClone(params.sharedStoreWrite ? shared : { ...local[params.agentDir], profiles: shared.profiles });
      params.updater(snapshot);
      if (params.sharedStoreWrite) shared = snapshot;
      else local[params.agentDir] = { ...snapshot, profiles: undefined };
      writes++;
      return snapshot;
    },
  };
  const agentSdk = {
    listAgentIds: () => ['main', 'worker'],
    resolveAgentDir: (_, id) => id,
    loadAuthProfileStoreWithoutExternalProfiles: id => structuredClone({ ...local[id], profiles: shared.profiles }),
  };
  return { request, sdk, agentSdk, auth, writeAuth, expires, root, local, shared: () => shared, writes: () => writes };
}

function addAccount(root, name) {
  const codexHome = path.join(root, name);
  fs.mkdirSync(codexHome);
  const auth = { auth_mode: 'chatgpt', tokens: { access_token: `${name}-access`, refresh_token: `${name}-refresh`, account_id: `${name}-account` } };
  fs.writeFileSync(path.join(codexHome, 'auth.json'), JSON.stringify(auth), { mode: 0o600 });
  return { name, codexHome };
}

test('all agents select the shared new profile; unrelated providers and credentials survive', async t => {
  const f = fixture(t); const result = await sync(f.request, f.sdk, f.agentSdk, {});
  assert.deepEqual(result.completed, ['main', 'worker']);
  for (const id of result.completed) assert.deepEqual(f.local[id].order.openai, [result.profileId]);
  assert.deepEqual(f.local.main.order.anthropic, ['keep-me']);
  assert.equal(f.shared().profiles['openai:old'].access, 'old-token');
  assert.equal(f.shared().profiles[result.profileId].refresh, 'fake-refresh');
  assert.equal(fs.statSync(result.backup).mode & 0o777, 0o700);
  for (const file of fs.readdirSync(result.backup)) assert.equal(fs.statSync(path.join(result.backup, file)).mode & 0o777, 0o600);
  assert(!JSON.stringify(result).includes('fake-refresh'));
  assert(!fs.readFileSync(path.join(result.backup, 'manifest.json'), 'utf8').includes('fake-access'));
});

test('dry run neither touches the auth store nor creates a backup', async t => {
  const f = fixture(t); f.agentSdk.loadAuthProfileStoreWithoutExternalProfiles = () => assert.fail('unexpected read');
  const result = await sync({ ...f.request, dryRun: true }, f.sdk, f.agentSdk, {});
  assert.equal(result.dryRun, true); assert.equal(f.writes(), 0);
  assert(!fs.existsSync(f.request.backupRoot));
});

test('agent scope leaves other agent orders alone', async t => {
  const f = fixture(t); const result = await sync({ ...f.request, agents: ['worker'] }, f.sdk, f.agentSdk, {});
  assert.deepEqual(result.completed, ['worker']); assert.deepEqual(f.local.main.order.openai, ['openai:old']);
});

test('unknown agents are rejected before any write', async t => {
  const f = fixture(t);
  await assert.rejects(sync({ ...f.request, agents: ['missing'] }, f.sdk, f.agentSdk, {}), /Unknown/);
  assert.equal(f.writes(), 0);
});

test('remote Gateway never receives a local credential mutation', async t => {
  const f = fixture(t);
  await assert.rejects(sync(f.request, f.sdk, f.agentSdk, { gateway: { mode: 'remote' } }), /Remote/);
  assert.equal(f.writes(), 0);
});

test('expired and API-key source logins are rejected', async t => {
  const f = fixture(t); f.sdk.resolveOpenAICodexAccessTokenExpiry = () => Date.now() - 1;
  await assert.rejects(sync(f.request, f.sdk, f.agentSdk, {}), /expired/);
  f.auth.auth_mode = 'apikey'; f.writeAuth();
  await assert.rejects(sync(f.request, f.sdk, f.agentSdk, {}), /OAuth/);
  assert.equal(f.writes(), 0);
});

test('newer OpenClaw refresh credentials cannot be overwritten by a stale source copy', async t => {
  const f = fixture(t); const first = await sync(f.request, f.sdk, f.agentSdk, {});
  const stored = f.shared().profiles[first.profileId];
  stored.expires += 3600_000; stored.access = 'new-access'; stored.refresh = 'new-refresh';
  await sync(f.request, f.sdk, f.agentSdk, {});
  assert.equal(f.shared().profiles[first.profileId].refresh, 'new-refresh');
});

test('ambiguous token divergence fails without overwriting the destination', async t => {
  const f = fixture(t); const first = await sync(f.request, f.sdk, f.agentSdk, {});
  f.shared().profiles[first.profileId].refresh = 'different-refresh';
  await assert.rejects(sync(f.request, f.sdk, f.agentSdk, {}), /diverged/);
  assert.equal(f.shared().profiles[first.profileId].refresh, 'different-refresh');
});

test('write failure reports a partial result and never includes raw SDK secrets', async t => {
  const f = fixture(t); const original = f.sdk.updateAuthProfileStoreWithLock;
  f.sdk.updateAuthProfileStoreWithLock = async params => {
    if (!params.sharedStoreWrite && params.agentDir === 'worker') throw new Error('secret=fake-refresh');
    return original(params);
  };
  await assert.rejects(sync(f.request, f.sdk, f.agentSdk, {}), error => {
    assert.match(error.message, /1\/2 agents/); assert(!error.message.includes('fake-refresh')); return true;
  });
});

test('an unavailable backup location blocks credential writes', async t => {
  const f = fixture(t); fs.writeFileSync(f.request.backupRoot, 'not a directory');
  await assert.rejects(sync(f.request, f.sdk, f.agentSdk, {}));
  assert.equal(f.writes(), 0);
});

for (const kind of ['readable', 'symlink', 'directory']) {
  test(`unsafe auth ${kind} is rejected before OpenClaw writes`, async t => {
    const f = fixture(t); const file = path.join(f.request.codexHome, 'auth.json');
    if (kind === 'readable') fs.chmodSync(file, 0o644);
    else if (kind === 'symlink') { fs.renameSync(file, file + '.real'); fs.symlinkSync(file + '.real', file); }
    else { fs.unlinkSync(file); fs.mkdirSync(file); }
    await assert.rejects(sync(f.request, f.sdk, f.agentSdk, {}), /auth.json/);
    assert.equal(f.writes(), 0);
    assert(!fs.existsSync(f.request.backupRoot));
  });
}

test('a single-account request without an accounts array still produces a one-element order', async t => {
  const f = fixture(t); const result = await sync(f.request, f.sdk, f.agentSdk, {});
  assert.deepEqual(result.profileIds, [result.profileId]);
  for (const id of result.completed) assert.deepEqual(f.local[id].order.openai, [result.profileId]);
});

test('pooled accounts: every agent gets every profile id, in the given order, and the previous order is backed up', async t => {
  const f = fixture(t);
  const second = addAccount(f.root, 'second');
  const request = { ...f.request, accounts: [{ name: 'main', codexHome: f.request.codexHome }, { name: 'second', codexHome: second.codexHome }] };
  const result = await sync(request, f.sdk, f.agentSdk, {});
  assert.deepEqual(result.completed, ['main', 'worker']);
  assert.equal(result.profileIds.length, 2);
  assert.notEqual(result.profileIds[0], result.profileIds[1]);
  for (const id of result.completed) assert.deepEqual(f.local[id].order.openai, result.profileIds);
  // Anthropic and other providers' orders are untouched by the pool sync.
  assert.deepEqual(f.local.main.order.anthropic, ['keep-me']);
  const agentBackup = JSON.parse(fs.readFileSync(path.join(result.backup, 'agent-0.json'), 'utf8'));
  assert.deepEqual(agentBackup.previousOrder, ['openai:old']);
  const manifest = JSON.parse(fs.readFileSync(path.join(result.backup, 'manifest.json'), 'utf8'));
  assert.deepEqual(manifest.accounts.map(a => a.name), ['main', 'second']);
});

test('one expired credential in the pool blocks the whole sync before any write', async t => {
  const f = fixture(t);
  const second = addAccount(f.root, 'second');
  f.sdk.resolveOpenAICodexAccessTokenExpiry = token => (token === 'second-access' ? Date.now() - 1 : f.expires);
  const request = { ...f.request, accounts: [{ name: 'main', codexHome: f.request.codexHome }, { name: 'second', codexHome: second.codexHome }] };
  await assert.rejects(sync(request, f.sdk, f.agentSdk, {}), /expired/);
  assert.equal(f.writes(), 0);
  assert(!fs.existsSync(request.backupRoot));
});
