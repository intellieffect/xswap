# xswap

**English** | [한국어](README.ko.md)

Local account management for Codex CLI, the macOS Codex/ChatGPT desktop app, and optional OpenClaw authentication sync. Register accounts you control, inspect remaining quota, and launch isolated profiles.

**Experimental:** automatic switching keeps a running Codex server and conversation while selecting another account from an explicit pool. It requires starting the session through xswap. It cannot retrofit an already-running ordinary session or automatically import its conversation history.

Independent community software. Not affiliated with or endorsed by OpenAI. Use your own authorized accounts and comply with your provider's terms and organization policies. xswap does not increase any account's quota.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and an installed Codex CLI. macOS and Linux are supported; desktop launching is macOS-only. Windows is not supported. No GitHub login is required for installation.

```sh
uv tool install 'git+https://github.com/intellieffect/xswap.git@v0.5.1'
xswap --version
```

If the command isn't found, run `uv tool update-shell` and open a new terminal. The package name is `intellieffect-xswap`; this release is installed from GitHub, not PyPI.

## Quick start

```sh
codex login                 # Skip if already signed in
xswap register main         # Reference the current login without copying it
xswap add work --use        # Sign in to a separate local profile and select it
xswap login work            # Re-authenticate work after its login expires
xswap use --best            # Select whichever signed-in account has the most quota right now
xswap                       # Launch the selected account's CLI
```

`main` and `work` are example local labels. Each account stores its own credentials and conversations. Credential storage must use Codex's `file` mode; `keyring` and `auto` modes are rejected without changing your configuration. The very first account added is selected automatically even without `--use`; `xswap add work` on its own leaves an existing selection unchanged.

```sh
xswap list                  # Live quotas, Spark hidden by default
xswap list --include-spark  # Also display Spark quotas
xswap usage work
xswap list --offline        # Local labels only
xswap app work              # Separate macOS desktop profile
xswap disable work          # Hold an account out of selection without deleting it
xswap enable work           # Restore it
xswap remove work           # Drop an account from xswap
```

Quota checks use Codex App Server, do not submit a model prompt, and may refresh the selected login through the official CLI. `xswap list --json` includes account labels and quota metadata: redact it before posting publicly. A disabled account is skipped by `list`'s live usage fetch, `use`, `--auto`/`auto-enable` pools, and `openclaw`, until re-enabled; it does not stop an explicit single-account launch like `xswap run --account NAME` or `xswap app NAME`.

`xswap remove` drops an account's registry entry but keeps its files by default; add `--purge` to also delete a managed profile's directory (a registered home, i.e. your own `~/.codex`, is never deleted regardless of flags). It refuses an account that is in an enabled `--auto`/`auto-enable` pool or in use by a running auto session; without `--yes` it asks for confirmation.

### Pick the account with the most quota

```sh
xswap run --best -- exec "summarize this repo"
```

`codex exec` and other non-interactive commands have no `--remote` hook, so the live auto bridge below cannot protect them. `--best` checks every signed-in account's remaining quota and launches with whichever has the most headroom, right before Codex starts. It is a one-shot choice made at launch, not live switching during the run; add `--model` to hint which quota to weigh, and `--dry-run` to see the choice and each candidate's remaining quota without launching.

To persist that choice as the default for later `xswap` and `xswap app` launches instead of a single run, use `xswap use --best` (also `--model` and `--openclaw`). It fails instead of keeping the current selection if no account has known remaining quota.

### Directory mappings

```sh
xswap map work ~/code/company   # Default to work whenever the cwd is inside ~/code/company
xswap map                       # List current mappings
xswap unmap ~/code/company      # Remove a mapping
```

The deepest matching directory wins. A mapping only applies where no account is named explicitly: precedence is explicit name (`--account`, a positional `NAME`) > directory mapping > the account selected with `xswap use`. Exactly these follow a mapping when no name is given: bare `xswap`, `xswap run` without `--account`, `xswap app`, `xswap status`, and `xswap usage`. `xswap openclaw` (name omitted) never follows a directory mapping — it always uses the account selected with `xswap use`, since it changes shared local OpenClaw agent state rather than a single launched session. A mapping to a disabled account still resolves and launches, consistent with `disable` only affecting `use`, `--auto`/`auto-enable` pools, `openclaw`, and live usage fetches.

## Alerts

```sh
xswap list --warn 15   # Warn on any codex window with less than 15% remaining
```

`--warn PCT` accepts 1-100. After the normal table (or `--json`) prints to stdout unchanged, xswap checks every non-disabled, successfully fetched account's `codex` windows and prints one `warn: NAME WINDOW N% left (resets ...)` line per stderr for each window below `PCT`. Unknown remaining values never warn. If any warning fired, `xswap list --warn` exits `3`; otherwise `0`. Plain `xswap list` is unaffected and always exits `0`.

This is meant to be polled from `launchd` or `cron`, not run interactively. `launchd`'s `PATH` does not include `/opt/homebrew/bin`, so point a wrapper script at absolute paths:

```sh
#!/bin/sh
# /Users/you/.local/bin/xswap-quota-check
/Users/you/.local/bin/xswap list --warn 15 \
  || /usr/bin/osascript -e 'display notification "Codex quota low" with title "xswap"'
```

Point your `launchd`/`cron` entry at that wrapper's absolute path; xswap does not schedule anything on its own.

## Status line

`xswap list --short` prints one line: each account as `{*}{name} {p5h}/{p7d}`, joined by ` · `, where `*` marks the active account and `p5h`/`p7d` are the Codex bucket's primary/secondary window remaining percentages (unknown is `?`). Disabled accounts are omitted from `list --short`, but `xswap usage <name> --short` always shows the named account, even if it is disabled; with no accounts, `list --short` prints an empty line. `--short` composes with `--offline`; it is mutually exclusive with `--json`. Use it in a tmux status line:

```sh
set -g status-right '#(xswap list --short)'
```

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

Each auto CLI run leaves a small record under `auto/cli-runs/`. As a side effect, `auto-status` prunes non-running records older than 7 days on every call, and `--prune` removes all non-running records immediately; either way, a record younger than 60 seconds is never removed, since it may still be between creation and its bridge taking its lock.

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
xswap upgrade                    # Reinstall the latest tag; add --tag vX.Y.Z for a specific release
xswap upgrade --dry-run          # Print the command without running it
```

Or run the underlying command directly:

```sh
uv tool install --force 'git+https://github.com/intellieffect/xswap.git@v0.5.1'
```

Before uninstalling, restore the optional wrapper:

```sh
xswap auto-disable
uv tool uninstall intellieffect-xswap
```

Account data is intentionally retained. These commands do not revoke credentials, undo an OpenClaw sync, or close running sessions.

## Troubleshooting

If something is broken, run `xswap doctor` first. It is read-only and makes no network calls: it checks the `codex` binary, the optional `codex` wrapper, credential storage mode, each registered account's login and token expiry, plugin links, the automatic-switching pool, and OpenClaw's plugin SDK. Use `xswap doctor --json` for machine-readable output; it prints nothing beyond local labels, paths, and short status text, and exits 1 if any check fails.

```sh
xswap doctor
```

## Contributing and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for local tests and fixture-only integration checks. Report reproducible bugs through [GitHub Issues](https://github.com/intellieffect/xswap/issues); report vulnerabilities privately through [Security Advisories](https://github.com/intellieffect/xswap/security/advisories/new).

[MIT license](LICENSE). OpenAI, Codex, ChatGPT, OpenClaw, and their distributed plugins remain governed by their own licenses and terms; their binaries or plugin code are not included in this repository.

### Weekly dashboard and macOS menu bar

`xswap list` and `xswap usage` show weekly used/remaining percentages as bars, selected account first. Colors are disabled for pipes, `NO_COLOR`, and dumb terminals. Missing weekly data stays unknown. `--details` shows identities and other quota windows; `--include-spark` adds Spark. `--json` preserves machine-readable quota data.

On macOS, run `xswap menubar` to compile and open `~/Applications/Xswap.app` using Apple Command Line Tools (`xcode-select --install` if missing). It shows the selected default account's weekly usage, all accounts, actual running bridge accounts, and the automatic-switch policy. It refreshes every five minutes and offers Refresh/Quit. Selection is not proof of a running session's account. Failed refreshes retain explicitly marked stale data. No login startup is configured. Quit the menu app before rebuilding after an upgrade, then run `xswap menubar` again. The app is built locally, not a notarized binary distribution.

`xswap switch NAME` (alias: `xswap use NAME`) now selects the default and sends a manual account change to **all running compatible auto-mode CLI/desktop bridges**. Idle bridges apply it immediately; busy bridges wait for all active turns to finish. The server process and conversation stay alive. The chosen registered account can be outside the automatic pool; the configured fallback pool and quota policy remain in effect for later turns.

```sh
xswap switch work
xswap auto-status                    # manualState: pending / applying / applied / failed
xswap switch work --default-only      # Only change the default for future launches
```

The command reports `applied`, `pending`, `unsupported`, `failed`, and `unconfirmed` counts. Only an acknowledged successful login counts as applied. Pending requests may take longer than the command's two-second acknowledgement window; check `auto-status`. Failure or unconfirmed delivery returns exit code 1; the saved default remains changed. Repeated requests to a busy bridge replace its pending selection with the latest one.

**Existing older bridges and ordinary account-specific CLI/app sessions cannot receive this update.** Open an auto-mode session once with the updated installation. Compatibility is advertised as `manualSwitchVersion: 1`; installing files does not modify running Python processes. No processes are killed, no conversation is restarted, and account credential files are not copied. Requests contain account names and per-process IDs only, in private local files. Authentication uses the existing [OpenAI App Server external-token login](https://learn.chatgpt.com/docs/app-server#3c-log-in-with-externally-managed-chatgpt-tokens-chatgptauthtokens).

Explicit `codex resume UUID` / `fork UUID` in automatic mode now locates that conversation in the current runtime, the original Codex home, or a registered account home. It reuses the owning home without copying the conversation and keeps the authentication bridge. The picker, named sessions, and `--last` remain scoped to the automatic runtime. Ambiguous duplicate UUIDs outside that runtime require choosing the original home explicitly.
