// Uses exported OpenClaw SDKs only; never patches its source or writes SQL.
import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

class BridgeError extends Error {}

function assert(condition, message) {
  if (!condition) throw new BridgeError(message);
}

function secureDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(directory);
  assert(!stat.isSymbolicLink() && stat.isDirectory() && stat.uid === process.getuid(), 'Unsafe backup directory');
  fs.chmodSync(directory, 0o700);
}

function writeBackup(file, data) {
  const fd = fs.openSync(file, 'wx', 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(data, null, 2));
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function readAuth(home) {
  let fd;
  try {
    fd = fs.openSync(path.join(home, 'auth.json'), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const stat = fs.fstatSync(fd);
    assert(stat.isFile() && stat.uid === process.getuid() && (stat.mode & 0o077) === 0,
      'Unsafe auth.json: require a user-owned regular file with mode 0600 (0400 also accepted), no symlink.');
    return JSON.parse(fs.readFileSync(fd, 'utf8'));
  } catch (error) {
    if (error instanceof BridgeError) throw error;
    throw new BridgeError('Cannot safely read auth.json; require a user-owned regular file, no symlink, and mode 0600.');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function sourceCredential(auth, sdk) {
  const tokens = auth.tokens;
  assert(auth.auth_mode === 'chatgpt' && tokens?.access_token && tokens?.refresh_token,
    'OpenClaw sync requires a ChatGPT OAuth login; API-key profiles are not supported.');
  const identity = sdk.resolveOpenAICodexAuthIdentity({ access: tokens.access_token, accountId: tokens.account_id });
  const claims = sdk.decodeOpenAICodexJwtPayload(tokens.access_token);
  const subject = claims?.sub;
  assert(identity.accountId && subject, 'Codex credential has no stable account identity.');
  const expires = sdk.resolveOpenAICodexAccessTokenExpiry(tokens.access_token);
  assert(Number.isFinite(expires) && expires > Date.now() + 60_000,
    'Codex access token is expired or about to expire. Open this account with xswap, complete a turn to refresh it, then retry.');
  const digest = createHash('sha256').update(identity.accountId + '\0' + subject).digest('hex').slice(0, 24);
  return {
    profileId: `openai:xswap-${digest}`,
    credential: { type: 'oauth', provider: 'openai', access: tokens.access_token, refresh: tokens.refresh_token,
      expires, ...identity, idToken: tokens.id_token },
  };
}

function preferredCredential(source, existing) {
  if (!existing) return source;
  assert(existing.type === 'oauth' && existing.provider === 'openai' && existing.accountId === source.accountId,
    'Target profile belongs to a different credential type or account; no overwrite performed.');
  // Never replace an OpenClaw refresh with an older copied Codex token.
  if (existing.expires > source.expires) return existing;
  if (existing.expires === source.expires) {
    assert(existing.access === source.access && existing.refresh === source.refresh,
      'Credentials diverged at the same expiry. Refresh/re-login the selected Codex account before retrying.');
    return existing;
  }
  return source;
}

function resolveAccounts(request) {
  if (Array.isArray(request.accounts) && request.accounts.length) return request.accounts;
  return [{ name: request.account, codexHome: request.codexHome }];
}

function backupSlug(profileId) {
  return profileId.replace(/[^a-zA-Z0-9_-]/g, '_');
}

export async function sync(request, sdk, agentSdk, config) {
  const accounts = resolveAccounts(request);
  // Source and validate every credential before any write: one bad account fails the whole pool.
  const sources = accounts.map(account => {
    const auth = readAuth(account.codexHome);
    const { profileId, credential } = sourceCredential(auth, sdk);
    return { name: account.name, profileId, credential };
  });
  const profileIds = sources.map(source => source.profileId);
  assert(config.gateway?.mode !== 'remote', 'Remote Gateways are not supported. Run xswap on the Gateway machine.');
  const available = agentSdk.listAgentIds(config);
  const ids = request.agents?.length ? [...new Set(request.agents)] : available;
  assert(ids.length > 0, 'No OpenClaw agents found.');
  for (const id of ids) assert(available.includes(id), `Unknown OpenClaw agent: ${id}`);
  const targets = ids.map(id => ({ id, agentDir: agentSdk.resolveAgentDir(config, id) }));
  // Dry-run performs no auth-store read/write, backup, or Gateway operation.
  const result = { account: request.account ?? sources[0]?.name, profileId: profileIds[0], profileIds,
    agents: ids, completed: [], dryRun: !!request.dryRun, backup: null };
  if (request.dryRun) return result;

  secureDirectory(request.backupRoot);
  const backup = path.join(request.backupRoot, new Date().toISOString().replaceAll(':', '-') + '-' + randomUUID().slice(0, 8));
  secureDirectory(backup);
  result.backup = backup;
  writeBackup(path.join(backup, 'manifest.json'), { version: 1, profileIds, targets,
    accounts: sources.map(source => ({ name: source.name, profileId: source.profileId })) });
  try {
    // OpenClaw owns the shared credential location; orders remain agent-scoped.
    const written = await sdk.updateAuthProfileStoreWithLock({
      agentDir: targets[0].agentDir, sharedStoreWrite: true,
      saveOptions: { filterExternalAuthProfiles: false, syncExternalCli: false },
      updater(store) {
        // Resolve every pooled credential against the current store BEFORE mutating any of
        // them: a divergence/type-mismatch on credential N must never leave credential N-1
        // written. This does not rely on the SDK discarding a partially mutated snapshot on
        // throw -- it never mutates `store` until every selection above has already succeeded.
        const selections = sources.map(source => {
          const existing = store.profiles[source.profileId] ?? null;
          return { source, existing, selected: preferredCredential(source.credential, existing) };
        });
        for (const { source, existing, selected } of selections) {
          writeBackup(path.join(backup, `credential-${backupSlug(source.profileId)}.json`), { profileId: source.profileId, previous: existing });
          store.profiles[source.profileId] = selected;
        }
        return true;
      },
    });
    assert(written !== null, 'Credential write failed; check OpenClaw storage and retry.');
    for (let index = 0; index < targets.length; index++) {
      const { id, agentDir } = targets[index];
      const visible = agentSdk.loadAuthProfileStoreWithoutExternalProfiles(agentDir);
      for (const profileId of profileIds) assert(visible.profiles[profileId], `Credential is not visible to ${id}.`);
      const updated = await sdk.updateAuthProfileStoreWithLock({
        agentDir,
        // Given order, as-is: OpenClaw rotates the pool itself via its own cooldowns.
        saveOptions: { preserveOrderProfileIds: profileIds, syncExternalCli: false },
        updater(store) {
          writeBackup(path.join(backup, `agent-${index}.json`), {
            agentId: id, agentDir, previousOrder: store.order?.openai ?? null,
          });
          store.order = { ...store.order, openai: [...profileIds] };
          return true;
        },
      });
      assert(updated !== null, `Auth order write failed for ${id}.`);
      const verified = agentSdk.loadAuthProfileStoreWithoutExternalProfiles(agentDir);
      assert(JSON.stringify(verified.order?.openai) === JSON.stringify(profileIds), `Auth order verification failed for ${id}.`);
      result.completed.push(id);
    }
    return result;
  } catch (error) {
    // Preserve honest partial state and a recovery point instead of undoing a concurrent token
    // refresh: the backup directory is never deleted on failure. Instead, mark it FAILED so a
    // partial backup (some credential-*.json/agent-*.json files, but not a completed sync) is
    // never mistaken for a clean recovery point.
    const detail = error instanceof BridgeError ? error.message : 'SDK or backup write failed; check storage permissions and free space, then retry.';
    try {
      const marker = path.join(backup, 'FAILED');
      if (!fs.existsSync(marker)) writeBackup(marker, { reason: detail, completed: result.completed });
    } catch { /* best effort; the thrown BridgeError below already reports the failure */ }
    throw new BridgeError(`OpenClaw sync incomplete (${result.completed.length}/${ids.length} agents). Backups: ${backup}. ${detail}`);
  }
}

async function main() {
  const request = JSON.parse(fs.readFileSync(0, 'utf8'));
  const require = createRequire(path.join(request.packageRoot, 'package.json'));
  const sdk = await import(pathToFileURL(require.resolve('openclaw/plugin-sdk/provider-auth')));
  const agentSdk = await import(pathToFileURL(require.resolve('openclaw/plugin-sdk/agent-runtime')));
  const configSdk = await import(pathToFileURL(require.resolve('openclaw/plugin-sdk/config-runtime')));
  for (const name of ['updateAuthProfileStoreWithLock', 'resolveOpenAICodexAuthIdentity', 'decodeOpenAICodexJwtPayload', 'resolveOpenAICodexAccessTokenExpiry'])
    assert(typeof sdk[name] === 'function', `Installed OpenClaw SDK lacks ${name}. Use OpenClaw 2026.8.1 or a compatible release.`);
  for (const name of ['listAgentIds', 'resolveAgentDir', 'loadAuthProfileStoreWithoutExternalProfiles'])
    assert(typeof agentSdk[name] === 'function', `Installed OpenClaw SDK lacks ${name}. Update OpenClaw.`);
  const result = await sync(request, sdk, agentSdk, configSdk.loadConfig());
  console.log(JSON.stringify(result));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await main(); }
  catch (error) {
    // No credential object, SDK stack, or raw SDK log is emitted.
    console.log(JSON.stringify({ error: error instanceof BridgeError ? error.message : 'Cannot load the selected credentials or OpenClaw SDK. Check the login file, installation, and storage permissions.' }));
    process.exitCode = 1;
  }
}
