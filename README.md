# xswap

**English** | [한국어](README.ko.md)

Local account management for Codex CLI, the macOS Codex/ChatGPT desktop app, and optional OpenClaw authentication sync. Register accounts you control, inspect remaining quota, and launch isolated profiles.

**Experimental:** automatic switching keeps a running Codex server and conversation while selecting another account from an explicit pool. It requires starting the session through xswap. It cannot retrofit an already-running ordinary session or automatically import its conversation history.

Independent community software. Not affiliated with or endorsed by OpenAI. Use your own authorized accounts and comply with your provider's terms and organization policies. xswap does not increase any account's quota.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and an installed Codex CLI. macOS and Linux are supported; desktop launching is macOS-only. Windows is not supported. No GitHub login is required for installation.

```sh
uv tool install 'git+https://github.com/intellieffect/xswap.git@v0.8.2'
xswap --version
```

If the command isn't found, run `uv tool update-shell` and open a new terminal. The package name is `intellieffect-xswap`; this release is installed from GitHub, not PyPI.

## 30-second tour

```sh
codex login                 # Skip if already signed in
xswap init                  # Register that login, add accounts, enable auto mode, run doctor
xswap list                  # Live quotas for every registered account
xswap use --best            # Select whichever signed-in account has the most quota right now
xswap                       # Launch the selected account's CLI
xswap auto-enable --accounts work,main --wrap-codex  # Auto-switch xswap, xswap app, and codex
xswap doctor                # Read-only health check; run this whenever something looks wrong
```

## What you get

- **Register accounts you control** and inspect remaining quota without submitting a model prompt or spending a turn — see [Quick start](#quick-start).
- **Launch whichever signed-in account has the most headroom**, once or as the persistent default — see [Pick the account with the most quota](#pick-the-account-with-the-most-quota).
- **Map a directory to an account** so a bare `xswap` always picks the right one for that project — see [Directory mappings](#directory-mappings).
- **Get warned before an account runs dry**, from a `launchd`/`cron` job or a between-session check — see [Alerts](#alerts).
- **Keep a running Codex server and conversation alive across accounts**, switching within an explicit pool at a weekly-remaining threshold — see [Automatic switching](#automatic-switching).
- **Sync ChatGPT credentials into a local OpenClaw Gateway**, optionally as a self-rotating pool — see [Optional OpenClaw sync](#optional-openclaw-sync).
- **Diagnose problems with one read-only, no-network command** — see [Troubleshooting](#troubleshooting).

## Table of contents

- [Install](#install)
- [Shell completion](#shell-completion)
- [Quick start](#quick-start)
- [Alerts](#alerts)
- [Status line](#status-line)
- [Automatic switching](#automatic-switching)
- [Storage and privacy](#storage-and-privacy)
- [Browser plugin repair](#browser-plugin-repair)
- [Optional OpenClaw sync](#optional-openclaw-sync)
- [Update or uninstall](#update-or-uninstall)
- [Troubleshooting](#troubleshooting)
- [Contributing and support](#contributing-and-support)
- [Weekly dashboard and macOS menu bar](#weekly-dashboard-and-macos-menu-bar)

## Shell completion

zsh: `xswap completion zsh > "${fpath[1]}/_xswap"`, or add `eval "$(xswap completion zsh)"` to `.zshrc`. Bash: add `eval "$(xswap completion bash)"` to `.bashrc`. Subcommands, options, and account names (read locally via `xswap list --offline`, no network) all complete.

## Quick start

```sh
codex login                 # Skip if already signed in
xswap init                  # Register that login, optionally add more accounts, enable auto mode, then run doctor
```

<details>
<summary>What xswap init actually runs</summary>

`xswap init` is a thin wizard over the commands below: it registers your existing `codex login` as `main` if it isn't registered yet, asks whether to add another account (blank to skip), offers `auto-enable --wrap-codex` once at least two accounts exist, offers `auto-policy --weekly-remaining 10`, and finishes by running `xswap doctor`. Each step is skippable and safe to run again. Add `--yes` to accept every safe default without prompting (registers `main` if eligible, adds no accounts, leaves auto mode off); `--no-auto` skips the automatic-switching step, and `--weekly-remaining PCT` overrides the policy default.

</details>

Or run the four steps by hand:

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

<details>
<summary>Quota-check and disabled-account semantics</summary>

Quota checks use Codex App Server, do not submit a model prompt, and may refresh the selected login through the official CLI. `xswap list --json` includes account labels and quota metadata: redact it before posting publicly. A disabled account is skipped by `list`'s live usage fetch, `use`, `--auto`/`auto-enable` pools, and `openclaw`, until re-enabled; it does not stop an explicit single-account launch like `xswap run --account NAME` or `xswap app NAME`.

</details>

<details>
<summary>What xswap remove and --purge actually delete</summary>

`xswap remove` drops an account's registry entry but keeps its files by default; add `--purge` to also delete a managed profile's directory (a registered home, i.e. your own `~/.codex`, is never deleted regardless of flags). It refuses an account that is in an enabled `--auto`/`auto-enable` pool, or that a running auto session holds — the account it is on right now *or* any account in that session's pool, which is the login it would move to when the current one hits the weekly limit. `--purge` deletes only the managed profile the registry actually names: when the recorded home is somewhere else (a registry restored or copied under another `CODEX_SWAP_HOME`, a hand-edited `home`), it refuses and names both paths rather than delete a directory the record does not name while the login it removed stays on disk. Without `--yes` it asks for confirmation.

</details>

### Pick the account with the most quota

```sh
xswap run --best -- exec "summarize this repo"
```

`codex exec` and other non-interactive commands have no `--remote` hook, so the live auto bridge below cannot switch them mid-run; with the `codex` command connected they run as the selected account (see Automatic switching), and `--best` is how you pick by quota instead. It checks every signed-in account's remaining quota and launches with whichever has the most headroom, right before Codex starts. It is a one-shot choice made at launch, not live switching during the run; add `--model` to hint which quota to weigh, and `--dry-run` to see the choice and each candidate's remaining quota without launching.

To persist that choice as the default for later `xswap` and `xswap app` launches instead of a single run, use `xswap use --best` (also `--model` and `--openclaw`). It fails instead of keeping the current selection if no account has known remaining quota.

### Directory mappings

```sh
xswap map work ~/code/company   # Default to work whenever the cwd is inside ~/code/company
xswap map                       # List current mappings
xswap unmap ~/code/company      # Remove a mapping
```

<details>
<summary>Precedence and which commands follow a mapping</summary>

The deepest matching directory wins. A mapping only applies where no account is named explicitly: precedence is explicit name (`--account`, a positional `NAME`) > directory mapping > the account selected with `xswap use`. Exactly these follow a mapping when no name is given: bare `xswap`, `xswap run` without `--account`, `xswap app`, `xswap status`, and `xswap usage`. `xswap openclaw` (name omitted) never follows a directory mapping — it always uses the account selected with `xswap use`, since it changes shared local OpenClaw agent state rather than a single launched session. A mapping to a disabled account still resolves and launches, consistent with `disable` only affecting `use`, `--auto`/`auto-enable` pools, `openclaw`, and live usage fetches.

</details>

## Alerts

```sh
xswap list --warn 15   # Warn on any codex window with less than 15% remaining
```

<details>
<summary>Exit codes and what counts as a warning</summary>

`--warn PCT` accepts 1-100. After the normal table (or `--json`) prints to stdout unchanged, xswap checks every non-disabled, successfully fetched account's `codex` windows and prints one `warn: NAME WINDOW N% left (resets ...)` line per stderr for each window below `PCT`. Unknown remaining values never warn. If any warning fired, `xswap list --warn` exits `3`; otherwise `0`. Plain `xswap list` is unaffected and always exits `0`. Outdated-bridge lines under Running sessions are part of the table, not `warn:` lines: they never change the exit code or post a notification.

</details>

This is meant to be polled from `launchd` or `cron`, not run interactively. `xswap alert --install` sets that up for you:

```sh
xswap alert --install --warn 15 --every 30
```

<details>
<summary>What xswap alert --install writes</summary>

On macOS this writes `~/.local/share/codex-swap/alert/run.sh` (a wrapper that calls the absolute `xswap` path resolved at install time, since `launchd`'s `PATH` lacks `/opt/homebrew/bin`, and turns each `warn:` line into an `osascript` notification) and `~/Library/LaunchAgents/com.intellieffect.xswap.alert.plist`, then loads it with `launchctl bootstrap`; each run's output lands in `alert/last.log`. The plist carries the state root this install was made for in `EnvironmentVariables` (`CODEX_SWAP_HOME`): launchd hands the job its own environment, so without it the job read the *default* root — another root's pool, reserve and selection than the one you installed it from, and with `--auto-switch` it moved that other root's selection every interval. The plist is created `0600` and `~/Library/LaunchAgents` `0700` when xswap creates it, and a re-install writes a new `run.sh` and renames it into place rather than rewriting the file a tick may be reading. Check it with `xswap alert --status` and remove it with `xswap alert --uninstall`. On non-macOS, `--install` prints an equivalent `cron` line instead of writing anything; it exports the same state root first.

</details>

### Proactive switching between sessions

Inside a session the bridge decides at turn start; nothing else moves the default selection, so a `codex exec` or a new session opened after the selected account ran down its week starts on an exhausted account. `xswap auto-tick` is the between-sessions check, meant for the same `launchd`/`cron` schedule as the alert:

```sh
xswap auto-tick --cached 600          # one check; exit 0 switched, 1 error, 2 no action, 3 blocked
xswap auto-tick --dry-run             # print the decision; select nothing, signal no bridge
xswap alert --install --auto-switch   # run it from the alert job, before the warn step
```

<details>
<summary>How auto-tick decides, and what it never does</summary>

It reads the pool and the weekly reserve from automatic switching (`xswap auto-enable`, `xswap auto-policy --weekly-remaining PCT`), fetches quota the way `xswap list` does -- reconnecting the wrapped `codex` entry if it needs it, exactly as `xswap list` would -- and applies the rule a bridge applies before a turn: when the account selected with `xswap use` is at or below the reserve (or its `codex` limit is reached, or its login needs a sign-in), it selects the pool account with the most headroom that is **above** the reserve, skipping disabled and sign-in-required accounts, through the same path as `xswap use NAME` -- the default for new sessions changes, the pool order puts that account first, and running bridges receive the manual-switch request (idle ones switch now, busy ones after their turn). It prints one `switched: OLD -> NEW (…)` line and then the bridge report. Unknown quota never causes a switch (`no-action:`, exit `2`), the same rule as `run --best` and the bridge; with automatic switching disabled, nothing selected, or no pool account above the reserve it prints `blocked:` and exits `3`; a manual `xswap use` that lands while quota is being read wins. No cooldown is needed: the rule compares only the selected account with the reserve, and weekly remaining only rises at its reset, so a switch cannot reverse until then. Directory mappings are not consulted. `--json` prints the decision with account names, percentages, and short reasons only.

With `--auto-switch`, `run.sh` runs `xswap auto-tick --cached SECONDS` before `list --warn` (so the table and warnings show the post-switch state from the same cached fetch) and turns a `switched:` line into a notification titled "xswap auto-switch"; `xswap alert --status` shows `auto-switch: on`. Re-run `--install` without the flag to drop the step. Automatic switching must be enabled for the tick to act; `--install` says so when it is not.

</details>

## Status line

`xswap list --short` prints one line: each account as `{*}{name} {p5h}/{p7d}`, joined by ` · `, where `*` marks the active account and `p5h`/`p7d` are the Codex bucket's primary/secondary window remaining percentages (unknown is `?`). Disabled accounts are omitted from `list --short`, but `xswap usage <name> --short` always shows the named account, even if it is disabled; with no accounts, `list --short` prints an empty line. `--short` composes with `--offline`; it is mutually exclusive with `--json`. Use it in a tmux status line:

```sh
set -g status-right '#(xswap list --short)'
```

### Cached lookups

<details>
<summary>Cache file, invalidation, and rejected-login records</summary>

`xswap list`, `xswap usage`, `xswap run --best`, and `xswap use --best` fetch live quota by default, spawning one `codex app-server` per account; add `--cached SECONDS` to reuse a still-fresh result instead, which matters for status-line and cron callers that check quota often. The default remains uncached unless `--cached` is passed. A successful fetch is always saved to `~/.local/share/codex-swap/usage-cache.json` (mode `0600`), keyed by account name, holding the same whitelisted fields shown on screen for that account — remaining percentages, reset times, plan type, credits, and the local account label (which can be an email address) — never raw server responses or tokens. A cache entry is only reused while its saved label still matches the account's current login; a re-login under the same name is treated as a miss. `--cached` cannot be combined with `--offline`, and on `run`/`use` it requires `--best`.

A login that the usage service rejects (a 401-class answer to the quota read, shown live as `sign-in required · xswap login NAME`) is also recorded, in `~/.local/share/codex-swap/auth-state.json` (mode `0600`; the account name, its local login label, the time, and the short reason — never tokens). While that record stands, `xswap list`/`usage` with `--cached` or `--offline` show `sign-in required · xswap login NAME` without spawning Codex, `run --best`/`use --best` and the automatic-switching pool skip the account without probing it, `xswap use NAME` refuses it, and `xswap doctor` reports it. A live `xswap list`/`usage` (no `--cached`) still tries again and a success clears the record; so does `xswap login NAME`. As with the usage cache, the record only counts while its saved label matches the account's current login.

</details>

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

Until the `codex` command is connected, `xswap use` prints which home and account plain `codex` still uses, and `xswap doctor` warns once two or more accounts are registered.

Two topics that live here get long enough to warrant their own page: which environment variables get dropped from a launched session and how the `codex` symlink is wrapped, re-wrapped, and resolved on PATH — see [docs/automatic-switching-internals.md](docs/automatic-switching-internals.md).

### What happens during a session

- Before an idle turn, xswap checks quota. At or below the configured weekly remainder, it selects an account **above** the same threshold. The default is `0` (exhaustion only). Seven-day windows are identified by duration, not their primary/secondary position.
- Active turns are not interrupted for a proactive switch. Usage can be cached for up to 30 seconds, so a long turn can still exhaust its quota.
- If Codex reports the structured `usageLimitExceeded` terminal error, xswap can change authentication and append a continuation to the **same thread** after observed active turns finish. It does not resend the original prompt or replay tool calls. Model actions are not guaranteed exactly-once.
- With no eligible candidate, a proactive switch leaves the current account selected; a quota-error continuation stops. Unknown quotas are not treated as available, and retries are bounded by account identity.
- An interrupt or new user request cancels a queued continuation. General network/authentication errors and arbitrary HTTP 429 errors are not treated as quota exhaustion.
- An account whose login the usage service rejected (recorded by any xswap quota read, see Cached lookups) is skipped as a candidate without being probed, and the reason is written to the session's status record. If that is the current account, the next idle turn moves to another pool member; a new session starts on the first pool member not recorded as rejected, and stops with `sign-in required; run: xswap login NAME` when every member is. `xswap login NAME` restores the account.
- After every login the bridge sends to its app server (session start, a proactive or quota-error switch, `xswap switch`, or an `xswap login` re-login), it reads the login back with `account/read` (local, no token refresh) and records the answer next to the self-reported `account` in the session's status record: `verifiedAccount`, `verifiedIdentity` (the address the server reports), `verifiedAt`. If the server holds no login or another one, the switch fails with reason `identity mismatch`, the previous account's login is re-sent, and the session stays on it; at session start the bridge stops with that reason instead of opening on an unknown account. When the server cannot answer (a Codex CLI without `account/read`, a timeout, a token without an email claim) the switch is kept, `verifiedAccount` stays `null`, and `verifyReason` says why. Plan type is never treated as identity: every account on a plan shares it.
- Between sessions nothing re-checks quota unless `xswap auto-tick` runs (see [Alerts](#alerts)); a new session or `codex exec` otherwise starts on the selected account as it is.

```sh
xswap auto-status
xswap auto-policy --weekly-remaining 15   # Running compatible bridges reread this between turns
xswap auto-disable                       # Restore the original codex symlink and future defaults
```

Disabling defaults does not terminate running sessions. Fixed-account commands (`xswap run --account main -- ...`, `xswap app main`) remain available.

<details>
<summary>Run-record pruning and bridge.log</summary>

Each auto CLI run leaves a small record under `auto/cli-runs/`. Records nobody will read again are removed as a side effect of `xswap auto-status`, the text output of `xswap list`/`usage` (so the `alert` job sweeps on its schedule), the menu bar's `dashboard` refresh, and every new bridged CLI launch: a record whose bridge ended cleanly (`stopped` without a `reason`) goes after 24 hours; a `stopped` record carrying a failure `reason`, or one whose bridge is gone without a `stopped` write (killed, crashed, rebooted), stays a week so the abnormal end remains visible; a directory holding nothing but a free lock file (the TUI exited before its bridge connected) goes after a minute. `auto-status --prune` removes every non-running record immediately. In every case a running bridge (its lock is held) and a record younger than 60 seconds are never removed, since a fresh record may still be between creation and its bridge taking its lock. `auto-status` lists what it removed under `prunedRuns` (run id, rule, age, account, last event, bridge version, stop reason); `list --json`/`--short`, `doctor`, and `upgrade` never remove anything.

Each run directory also holds a `bridge.log`: one line per bridge event (local timestamp, event, account, the requested account or candidate, a short reason, and the exception type when something failed), created with mode 0600 and kept under 1 MiB by dropping its oldest lines. Failures are classified into short reasons -- for example `usage service unavailable`, `ChatGPT access token needs refresh or sign-in`, `selected account is disabled or removed`, `Codex rejected account/login/start`, `app-server request timed out` -- and never contain tokens, prompts, or raw errors. `xswap auto-status` shows the same reason as `manualReason` (why a manual switch is pending or failed, kept until the next request), `lastFailure` (the most recent failed event of the session), and each session's `log` path; `xswap use`/`switch`/`login` print it as `Failed: ...`, and a CLI session names the log when it exits after a failure.

</details>

### Compatibility and limits

| Surface | Support |
|---|---|
| Interactive CLI, including resume/fork | Auto mode through a private local Unix WebSocket |
| macOS desktop app | Auto mode through `CODEX_CLI_PATH` and stdio |
| `codex exec`, `review`, and other non-interactive commands | Run once as the xswap-selected account (directory mapping, then `xswap use`) when the `codex` command is connected; no live switching during the run |
| `login`, `logout`, `app`, `app-server`, `completion`, `help`, `update`, `upgrade`, `--help`/`--version`/`--remote` | Passed through with your own home |
| Already-running ordinary sessions | Cannot be attached or upgraded in place |
| Bridged sessions open during `xswap upgrade` | Keep the previous bridge until reopened; `list`, `doctor`, and `upgrade` name the resume command |
| Existing ordinary conversation history | Not copied into auto mode; `resume --last` searches the auto-mode store |
| Between sessions (new sessions, `codex exec`) | `xswap auto-tick` from the alert job moves the selection; there is no daemon |
| OpenClaw | Explicit one-time sync only, not the CLI/desktop auto-switching loop |

The implementation depends on experimental Codex external-auth/remote interfaces and desktop environment variables. Compatibility may change with updates. CLI 0.153.4 and a macOS desktop-bundled Codex were tested against local fake-auth fixtures; **real subscription quota exhaustion is not part of automated validation**. A real backend/account deployment still requires its own verification.

## Storage and privacy

Default data root: `~/.local/share/codex-swap/` (override with `CODEX_SWAP_HOME`). That variable decides which `auto.json` a shell's `xswap` *and* its `codex` command read — the registered accounts, the selection, and the record that connects `codex` all live under it — so a shell, launchd job or desktop app without it reads the default root and follows whatever selection is there, or none. It is named where it decides that: `xswap auto-enable --wrap-codex` prints which root it put the record in and what a shell without the variable reads instead, `xswap doctor` adds a `state root` row when the variable chose the root (OK when it merely names the default one), and plain `codex` names the root it read when it cannot find the real Codex — a failure that no longer creates an empty default root on its way out.

| Path | Purpose |
|---|---|
| `accounts.json` | Local profile names and source-home paths |
| `profiles/<name>/codex/` | Account credentials and conversations |
| `auto/codex/`, `auto/cli-codex/` | Separate desktop and CLI auto-mode conversations |
| `auto.json`, `auto/**/status.json` | Pool, wrapper recovery (`wrapper`, plus `wrappers` for previously wrapped entries), threshold, operational status, and the login label each session's app server confirmed |
| `auto/*/packages`, `profiles/<name>/codex/packages` | Links to the reference Codex home's `packages`, so a Codex update from inside a session installs there, never here |
| `auto/**/bridge.log` | Per-run bridge event log: timestamps, events, local account labels, short reasons; no tokens, prompts, or raw errors |
| `auth-state.json` | Accounts whose login the usage service rejected: name, local login label, time, short reason |
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
xswap openclaw --pool main,work
```

This explicitly copies ChatGPT OAuth credentials into OpenClaw's auth store, selects them for the requested local agents, saves private recovery backups, and reloads Gateway auth. It is not automatic synchronization and does not submit a verification prompt. API-key profiles and remote Gateways are unsupported. Partial updates are reported with backup information; do not publish those backups.

<details>
<summary>Pool-sharing and cross-organization refusal rules</summary>

`--pool a,b,c` (two or more distinct registered names; mutually exclusive with a positional NAME) copies every listed account's credentials into OpenClaw and sets each target agent's OpenAI auth order to the given pool, in that order. OpenClaw then rotates within that order on its own cooldowns (`resolveAuthProfileOrder`, `isProfileInCooldown`, `markAuthProfileFailure`/`markAuthProfileCooldown`) — no human has to run `xswap openclaw other` to bring a rate-limited bot back. Because a pooled agent shares one conversation/task context across whichever account it is currently using, pooling only makes sense for accounts that may see each other's context; sync refuses to pool accounts from different ChatGPT organizations unless you pass `--allow-mixed`. If an account's organization can't even be determined (unreadable credential, no decodable claim), the pool is refused the same way an actual mismatch would be refused — an unknown organization is never treated as a match.

</details>

### Stale OpenClaw cooldown

OpenClaw blocks an OpenAI auth profile for the rest of its rate-limit window after a single 429, and never re-checks it before that window ends. If the underlying limit actually clears earlier (a plan upgrade, a manual reset upstream), the profile stays locked out anyway — the symptom is `openai usage: 100% left` in `xswap list` sitting next to `[cooldown 6d]` in `openclaw models status`. `xswap doctor` reports this as a `NAME: openclaw cooldown` check: `FAIL` when the cooldown is active but xswap's own usage cache shows real quota remaining, `WARN` when it's in cooldown but the remaining amount isn't known locally, and it never fails a disabled account.

```sh
xswap openclaw NAME --clear-cooldown --dry-run
xswap openclaw NAME --clear-cooldown
xswap openclaw --pool a,b --clear-cooldown --yes
```

This stops the local Gateway (`openclaw gateway stop --force`), backs up its state database privately, clears only the stale `blockedUntil`/`blockedReason`/`blockedSource` keys (resetting the error count to 0) for the named account(s) — or every registered account when NAME/`--pool` is omitted — and starts the Gateway again. A failed Gateway stop aborts before anything is written; `--dry-run` only lists what would be cleared and never touches the Gateway or the database.

## Update or uninstall

```sh
xswap upgrade                    # Reinstall the latest tag; add --tag vX.Y.Z for a specific release
xswap upgrade --dry-run          # Print the command without running it
```

After a successful install, `xswap upgrade` lists every running bridged session with the command that reopens it on the new code (see Automatic switching). Nothing is stopped or restarted for you.

Or run the underlying command directly:

```sh
uv tool install --force 'git+https://github.com/intellieffect/xswap.git@v0.8.2'
```

Before uninstalling, restore the optional wrapper:

```sh
xswap auto-disable
uv tool uninstall intellieffect-xswap
```

Account data is intentionally retained. These commands do not revoke credentials, undo an OpenClaw sync, or close running sessions.

## Troubleshooting

<details>
<summary>Everything xswap doctor checks</summary>

If something is broken, run `xswap doctor` first. It is read-only and makes no network calls: it checks the `codex` binary, every `codex` on PATH against the optional wrapper, credential storage mode, each registered account's login, token expiry, and any rejected login recorded by a quota read, plugin links, the automatic-switching pool, where the real Codex executable lives (`real codex`, `packages link`), running bridged sessions still on an older bridge than the installed xswap, running auto sessions' server-confirmed login, and OpenClaw's plugin SDK. A `codex` entry a Codex update re-pointed at another existing executable is reported as FAIL until the next `xswap` launch, `list`, or `use` reconnects it; an entry xswap cannot wrap — its target missing or not executable, a regular file where the symlink was, a link owned by another user, a `codex` reached through a relative PATH entry, or automatic switching disabled — stays FAIL until you apply the fix the row names, and `xswap doctor` keeps exiting 1 until then. The `codex binary` row applies the same rule to the lookup it makes itself: a `codex` found only through a relative PATH entry is FAIL naming that entry, rather than being run -- and then reported as this machine's Codex -- from whichever directory doctor was started in. Two rows report the environment rather than a file: `codex bypass` when `XSWAP_BYPASS=1` is set in the shell doctor runs in, and `state root` when `CODEX_SWAP_HOME` chose which state root this shell reads. Use `xswap doctor --json` for machine-readable output; it prints nothing beyond local labels, paths, and short status text, and exits 1 if any check fails.

</details>

```sh
xswap doctor
```

## Contributing and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for local tests and fixture-only integration checks. Report reproducible bugs through [GitHub Issues](https://github.com/intellieffect/xswap/issues); report vulnerabilities privately through [Security Advisories](https://github.com/intellieffect/xswap/security/advisories/new).

[MIT license](LICENSE). OpenAI, Codex, ChatGPT, OpenClaw, and their distributed plugins remain governed by their own licenses and terms; their binaries or plugin code are not included in this repository.

### Weekly dashboard and macOS menu bar

`xswap list` and `xswap usage` group each account into a tree with its email, a weekly remaining-quota gauge (`█` remaining, `░` used), and a local reset date/time with countdown. Slots stay in registration order; switching changes only the selection marker. Detected running xswap bridge sessions appear separately, grouped by account and surface. Colors are disabled for pipes, `NO_COLOR`, and dumb terminals. Missing weekly data stays unknown. `--details` adds other quota windows and reset credits; `--include-spark` adds Spark. `--json` preserves machine-readable quota data. `xswap list` defaults to English regardless of locale; use `--lang ko` explicitly for Korean. Other views retain their `--lang` / `XSWAP_LANG` / locale language settings.

On macOS, run `xswap menubar` to compile and open `~/Applications/Xswap.app` using Apple Command Line Tools (`xcode-select --install` if missing). It shows the selected default account's weekly usage, all accounts, actual running bridge accounts, and the automatic-switch policy. It refreshes every five minutes and offers Refresh/Quit. Selection is not proof of a running session's account. Failed refreshes retain explicitly marked stale data. No login startup is configured. Quit the menu app before rebuilding after an upgrade, then run `xswap menubar` again. The app is built locally, not a notarized binary distribution. Its own launch environment (not the invoking shell's) decides English vs. Korean the same way `--lang`/`XSWAP_LANG` does, and it passes that choice explicitly to the `xswap dashboard` it calls internally.

Manual `xswap switch`/`use` broadcasts to running bridges, resume/fork across accounts, and the run-record bookkeeping behind them are covered in [docs/session-switching-details.md](docs/session-switching-details.md).
