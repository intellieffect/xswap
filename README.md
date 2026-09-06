# xswap

**English** | [한국어](README.ko.md)

Local account management for Codex CLI, the macOS Codex/ChatGPT desktop app, and optional OpenClaw authentication sync. Register accounts you control, inspect remaining quota, and launch isolated profiles.

**Experimental:** automatic switching keeps a running Codex server and conversation while selecting another account from an explicit pool. It requires starting the session through xswap. It cannot retrofit an already-running ordinary session or automatically import its conversation history.

Independent community software. Not affiliated with or endorsed by OpenAI. Use your own authorized accounts and comply with your provider's terms and organization policies. xswap does not increase any account's quota.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and an installed Codex CLI. macOS and Linux are supported; desktop launching is macOS-only. Windows is not supported. No GitHub login is required for installation.

```sh
uv tool install 'git+https://github.com/intellieffect/xswap.git@v0.4.3'
xswap --version
```

If the command isn't found, run `uv tool update-shell` and open a new terminal. The package name is `intellieffect-xswap`; this release is installed from GitHub, not PyPI.

## Quick start

```sh
codex login                 # Skip if already signed in
xswap register main         # Reference the current login without copying it
xswap add work              # Sign in to a separate local profile
xswap use work
xswap                       # Launch the selected account's CLI
xswap login work            # Re-authenticate work after its login expires
```

`main` and `work` are example local labels. Each account stores its own credentials and conversations. Credential storage must use Codex's `file` mode; `keyring` and `auto` modes are rejected without changing your configuration.

```sh
xswap list                  # Live quotas, Spark hidden by default
xswap list --include-spark  # Also display Spark quotas
xswap usage work
xswap list --offline        # Local labels only
xswap app work              # Separate macOS desktop profile
```

Quota checks use Codex App Server, do not submit a model prompt, and may refresh the selected login through the official CLI. `xswap list --json` includes account labels and quota metadata: redact it before posting publicly.

## Automatic switching

Start with an explicit pool of at least two signed-in accounts:

```sh
xswap auto-policy --weekly-remaining 10
xswap run --auto --accounts work,main
# Or: xswap app --auto --accounts work,main
```

To use that pool for future plain `xswap` and `xswap app` launches:

```sh
xswap auto-enable --accounts work,main
```

Optionally connect the ordinary `codex` command too:

```sh
xswap auto-enable --accounts work,main --wrap-codex
```

Wrapping requires a user-owned `codex` symlink and saves its original target. A regular executable isn't overwritten. If wrapping isn't supported by your installation, use `xswap` directly. Codex updates may replace the symlink, requiring you to reconnect it.

### What happens during a session

- Before an idle turn, xswap checks quota. At or below the configured weekly remainder, it selects an account **above** the same threshold. The default is `0` (exhaustion only). Seven-day windows are identified by duration, not their primary/secondary position.
- Active turns are not interrupted for a proactive switch. Usage can be cached for up to 30 seconds, so a long turn can still exhaust its quota.
- If Codex reports the structured `usageLimitExceeded` terminal error, xswap can change authentication and append a continuation to the **same thread** after observed active turns finish. It does not resend the original prompt or replay tool calls. Model actions are not guaranteed exactly-once.
- With no eligible candidate, a proactive switch leaves the current account selected; a quota-error continuation stops. Unknown quotas are not treated as available, and retries are bounded by account identity.
- An interrupt or new user request cancels a queued continuation. General network/authentication errors and arbitrary HTTP 429 errors are not treated as quota exhaustion.

```sh
xswap auto-status
xswap auto-policy --weekly-remaining 15   # Running compatible bridges reread this between turns
xswap auto-disable                       # Restore the original codex symlink and future defaults
```

Disabling defaults does not terminate running sessions. Fixed-account commands (`xswap run --account main -- ...`, `xswap app main`) remain available.

### Compatibility and limits

| Surface | Support |
|---|---|
| Interactive CLI, including resume/fork | Auto mode through a private local Unix WebSocket |
| macOS desktop app | Auto mode through `CODEX_CLI_PATH` and stdio |
| `codex exec`, login/logout, utility commands | Passed through; no automatic switching |
| Already-running ordinary sessions | Cannot be attached or upgraded in place |
| Existing ordinary conversation history | Not copied into auto mode; `resume --last` searches the auto-mode store |
| OpenClaw | Explicit one-time sync only, not the CLI/desktop auto-switching loop |

The implementation depends on experimental Codex external-auth/remote interfaces and desktop environment variables. Compatibility may change with updates. CLI 0.153.4 and a macOS desktop-bundled Codex were tested against local fake-auth fixtures; **real subscription quota exhaustion is not part of automated validation**. A real backend/account deployment still requires its own verification.

## Storage and privacy

Default data root: `~/.local/share/codex-swap/` (override with `CODEX_SWAP_HOME`).

| Path | Purpose |
|---|---|
| `accounts.json` | Local profile names and source-home paths |
| `profiles/<name>/codex/` | Account credentials and conversations |
| `auto/codex/`, `auto/cli-codex/` | Separate desktop and CLI auto-mode conversations |
| `auto.json`, `auto/**/status.json` | Pool, wrapper recovery, threshold, and operational status |
| `backups/openclaw/` | Sensitive backups from explicitly requested OpenClaw sync |

Auto mode reads tokens from their original account homes and sends them to its local child server in memory; it does not copy them into the auto conversation home. xswap's status records exclude tokens and prompts, but contain local account labels/PIDs. Codex itself persists conversation data in its normal runtime store. xswap adds no telemetry service.

Profile separation is **not a security sandbox**: configurations, skills, and rules may be shared. All accounts in an auto pool share that session's task context. Do not mix unrelated people or organizations in a pool. Never publish auth files, raw session data, credential backups, or unredacted diagnostics. See [SECURITY.md](SECURITY.md).

## Browser plugin repair

Legacy xswap versions shared `plugins` via symlinks, which could escape the runtime's trusted `CODEX_HOME` boundary. Current profiles use local plugin caches; plugin changes are managed independently per home.

```sh
xswap repair-plugins --dry-run
xswap repair-plugins
```

Repair atomically exchanges known legacy links with local code directories without expanding trust roots or stopping conversations. Source files and existing local plugin directories are preserved; external, broken, and cyclic cache links are rejected. Cached browser initialization failures require the browser tool's supported `js_reset` followed by initialization again. That clears its JavaScript state, not the Codex conversation. Repair does not reset other sessions automatically. Browser-service setup fixtures do not verify live browser hosting or navigation.

## Optional OpenClaw sync

Requires a local running OpenClaw Gateway, `openclaw`, and `node` in PATH. Tested with OpenClaw 2026.8.1's public plugin SDK; incompatible SDKs fail with an error.

```sh
xswap openclaw work --dry-run
xswap openclaw work
```

This explicitly copies ChatGPT OAuth credentials into OpenClaw's auth store, selects them for the requested local agents, saves private recovery backups, and reloads Gateway auth. It is not automatic synchronization and does not submit a verification prompt. API-key profiles and remote Gateways are unsupported. Partial updates are reported with backup information; do not publish those backups.

## Update or uninstall

```sh
uv tool install --force 'git+https://github.com/intellieffect/xswap.git@v0.4.3'
```

Before uninstalling, restore the optional wrapper:

```sh
xswap auto-disable
uv tool uninstall intellieffect-xswap
```

Account data is intentionally retained. These commands do not revoke credentials, undo an OpenClaw sync, or close running sessions.

## Contributing and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for local tests and fixture-only integration checks. Report reproducible bugs through [GitHub Issues](https://github.com/intellieffect/xswap/issues); report vulnerabilities privately through [Security Advisories](https://github.com/intellieffect/xswap/security/advisories/new).

[MIT license](LICENSE). OpenAI, Codex, ChatGPT, OpenClaw, and their distributed plugins remain governed by their own licenses and terms; their binaries or plugin code are not included in this repository.
