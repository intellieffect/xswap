# Changelog

## 0.8.0

Hardening follow-up to the 2026-09-10 bypass incident, from a source-level comparison with claude-swap (INT-5186). Eleven gaps where xswap could not see, remember, or report a failure that happened outside a bridged session.

- Stale CLI run records are pruned where a session naturally starts or is looked at, not only by `auto-status` (audit item 5: this Mini held 18 `auto/cli-runs/` records for one running session, one of them an empty directory invisible to every report). The text output of `xswap list`/`usage` (so the `alert` job sweeps on its schedule), the menu bar's `dashboard`, and every new bridged CLI launch now sweep `auto/cli-runs/`: a cleanly `stopped` record goes after 24 hours, a `stopped` record carrying a failure `reason` or one whose bridge vanished without a `stopped` write keeps its 7-day history, and a directory holding nothing but a free lock file goes after a minute. Running bridges and records under 60 seconds old are still never touched; `list --json`/`--short`, `doctor`, and `upgrade` stay read-only. `auto-status` reports each removal under `prunedRuns` (`run`, `rule` of `stopped`/`stale`/`empty`/`forced`, `ageSeconds`, `account`, `event`, `bridgeVersion`, `reason`); `pruned` still counts them. A record removed by a concurrent sweep mid-scan is skipped instead of crashing the report.
- **Wrapper drift is no longer silent when xswap cannot act.** 0.7.8 reconnected the `codex` entry after a Codex update only when the link was user-owned and pointed at an existing executable while automatic switching was on; every other case (dangling or non-executable target, a regular file or a foreign-owned link where the symlink was, a missing entry, or switching disabled) was skipped without a word, and `xswap doctor` still promised that "the next xswap launch, list, or use reconnects it". `wrapper_drift` now classifies the entry (`ok`, `replaced`, `not-wrapped`, `missing`, `not-a-symlink`, `not-user-owned`, `dangling-target`, `not-executable`, `auto-disabled`, `unreadable`); the reconnect behaviour itself is unchanged and still prints nothing on a skip. `xswap doctor`'s wrapper row names the reason and the manual fix (`codex command not connected (dangling-target): ... Fix: ...`), and after `xswap auto-disable` it reads like "not connected" instead of "changed outside xswap". `xswap use`/`switch` print the same reason once in their notice, and `xswap auto-status` adds `wrapperReason` next to `codexWrapped` as a read-only diagnostic field (nothing inside xswap reads it in 0.8.0; it is the only way to read the reason without parsing doctor's text).

- **A `codex` entry ahead of the wrapped one on PATH no longer bypasses xswap unnoticed.** On 2026-09-10 the standalone installer's `~/.local/bin/codex` preceded the wrapped `/opt/homebrew/bin/codex`: plain `codex` ran the standalone binary against `~/.codex`, and `xswap doctor` said OK because it only inspected the entry recorded in `auto.json`. `xswap doctor`'s wrapper row now walks every `codex` on PATH in lookup order (one per directory, the way the shell resolves it). With automatic switching on: FAIL when the first entry is not `xswap-codex`, naming the entry, its target, and whether the next launch repairs it; WARN when a later entry is a different `codex` (it runs only by full path, or in a job whose PATH differs); OK lists the count when several entries are all wrapped. A recorded entry that a Codex update re-pointed is now FAIL instead of WARN -- plain `codex` is bypassing xswap at that moment, so `xswap doctor` exits 1 until the next launch, `list`, or `use` reconnects it. After `auto-disable` the row reads "not connected" instead of the previous false "changed outside xswap". PATH is the one of the shell running doctor.
- `xswap use`/`switch` use the same lookup: the notice names the entry plain `codex` actually runs and, for a regular file or a link owned by another user, says xswap cannot wrap it.
- While automatic switching is enabled, every `xswap` launch, `list`/`usage`, `use`/`switch`, and bridged-session end also wraps a new user-owned `codex` symlink that appeared ahead of the wrapped entry, under the 0.7.8 reconnect rule (an existing executable, not `xswap-codex`), and only when the wrapped entry is on the same PATH -- nothing is re-pointed from a launchd or test PATH. That entry becomes the primary `wrapper` record; the previous record moves to a new `wrappers` list in `auto.json`, and `xswap auto-enable --wrap-codex` over an already-wrapped install keeps the previous record the same way. `xswap auto-disable` restores every record. `wrapper` keeps its shape, so 0.7.x code and running bridges read it unchanged.
- `auto-status`'s `codexWrapped` follows the PATH lookup.
- **The real Codex no longer lives inside xswap's state directory** (INT-5186, item 2). Codex's standalone installer derives every path from `CODEX_HOME` (`STANDALONE_ROOT="$CODEX_HOME_DIR/packages/standalone"`, and `~/.local/bin/codex` is linked to `"$CURRENT_LINK/bin/codex"` literally), so an update started inside a bridged session -- where `CODEX_HOME` is xswap's runtime home -- installed its release under `auto/cli-codex/packages`, and 0.7.8 recorded that path as `realCodex` (Mini, 2026-09-13: both 0.153.4 and 0.154.0 lived only there; `~/.codex/packages` did not exist). Purging or resetting `auto/` would have taken plain `codex` down with it.
  - Every home xswap hands to Codex (`auto/cli-codex`, `auto/codex`, `profiles/<name>/codex`) now gets `packages` as a link to the reference Codex home's `packages` -- the registered home outside xswap's state that already holds the real Codex, else `~/.codex` (`CODEX_HOME`) -- created when the home is prepared or launched and only if the entry is absent. Updates from inside a session therefore install where they would without xswap; a `codex` link the updater spells through that path is recorded via the reference home, never through `auto/`.
  - `xswap doctor` adds a `real codex` row: FAIL when the recorded executable is inside xswap's state (literally or once links resolve) with `Fix: xswap relocate-codex`, FAIL with the recovery steps when it is missing, OK with the path otherwise; and `packages link: <home>` rows (WARN for a real directory, and for a link that is broken or stays inside the state directory).
  - `xswap relocate-codex [--dry-run]` moves releases already inside such a home to the reference home's `packages/standalone/releases/`, re-points `current`, replaces the directory with the link, rewrites `realCodex`/`originalTarget` in `auto.json`, and re-points a `codex` link that `auto-disable` had restored. It never deletes a release, refuses before changing anything when the destination already exists, when the layout is not the installer's (`.staging.*`, `install.lock.d`, unknown entries), or when no reference home outside xswap's state exists, and edits no shell configuration. Running bridged sessions keep a valid binary path through the new link.
  - Plain `codex` (through `xswap-codex`), every `xswap` command that spawns Codex, and `xswap auto-disable` now say explicitly when the real Codex executable is missing -- the path, that plain `codex` cannot start, and `xswap auto-disable`, reinstall Codex, `xswap auto-enable --accounts ... --wrap-codex` as the recovery -- instead of the generic "could not start" line or a silently dangling link.
- **`codex exec` and the other non-interactive commands now follow the xswap selection** (INT-5186, audit item 3). With the `codex` command connected, `exec`, `review`, `mcp`, `mcp-server`, `apply`, `cloud`, and every other subcommand without a `--remote` hook were handed to the real Codex with the caller's environment untouched, so they ran against `~/.codex` and whichever account was signed in there — silently, while interactive `codex` in the same shell used the pool. They now run once as the account `xswap run` resolves (directory mapping for the current directory, else `xswap use`), with `CODEX_HOME` set to that home and `OPENAI_API_KEY`/`CODEX_API_KEY`/`CODEX_ACCESS_TOKEN`/workload-identity variables dropped like every other xswap launch, and print `xswap: running codex exec as NAME` on stderr (`XSWAP_QUIET=1` silences only that line). There is no live switching during the run. An explicit `CODEX_HOME`, `XSWAP_BYPASS=1`, `--help`/`--version`/`--remote` forms, and `login`, `logout`, `app`, `app-server`, `completion`, `help`, `update`, `upgrade` keep the caller's own home without a notice (`codex update`/`codex upgrade` install releases under `$CODEX_HOME/packages/standalone/`, so they must not run inside a profile). A disabled, not-signed-in, or otherwise unusable selection falls back to the caller's own home with a one-line warning naming the fix; with automatic switching disabled the wrapper stays transparent. `codex exec` sessions saved before 0.8.0 remain under `~/.codex/sessions`; resume them with `XSWAP_BYPASS=1 codex exec resume …`.
  - `codex upgrade` was in neither subcommand set, so it counted as interactive: a wrapped `codex upgrade` ran inside the bridge, whose `CODEX_HOME` is `auto/cli-codex`, and installed the release into xswap's own state -- the accident `xswap relocate-codex` exists to undo. It is now classified with `update`, so the wrapper passes it through to the caller's home and `xswap run -- upgrade` launches it directly in the profile home, where item 2's `packages` link sends the release to the reference Codex home.

- **Bridge failures now say why** (INT-5186, item 4). On 2026-09-10 three manual switches ended as `manualState: failed` with `reason: null` and nothing else was recorded, so the cause could not be reconstructed: `failure_reason()` exposed only an exception type and there was no bridge log. Every failure the bridge used to swallow -- a failed or pending manual switch, an automatic candidate that could not be used or was skipped, a failed continuation after a usage limit, a failed token refresh, a failed pre-turn quota check -- now records a short classified `reason` in `status.json` (`usage service unavailable`, `ChatGPT access token needs refresh or sign-in`, `selected account is disabled or removed`, `Codex rejected account/login/start`, `app-server request timed out`, `account config.toml is unreadable`, `local I/O error: ...`), and so does the `stopped` record when the bridge itself died. `manualReason` stays next to `manualState` until the next request, `lastFailure` keeps the newest failed event until the bridge exits -- a later `stopped` never replaces it, so a bridge that dies after a failed switch still names the switch, and `bridge.log` keeps the whole order -- and `no-available-account` lists every candidate with its reason (`second: no quota above the weekly reserve; third: quota unknown`). New events: `candidate-skipped`, `quota-check-failed`.
- Each CLI run directory (and `auto/` for the desktop bridge) gets a `bridge.log`, mode 0600: one line per event with local timestamp, event, account, candidate, reason, and exception type; never tokens, prompts, or raw errors. It is capped at 1 MiB by keeping the newest 1000 lines, is only written through a user-owned regular file, and is removed with its run record.
- `xswap auto-status` shows `manualReason`, `lastFailure`, `candidate`, and each session's `log` path. `xswap use`/`switch`/`login` add `Failed: <reason>` when a running bridge rejects the request. After a bridged CLI session exits, the disconnect message names the log, and a session that ended normally after a failure prints `xswap auto: last failure in this session: ... ; see <run>/bridge.log`.
- Bridged sessions that keep running the previous code after `xswap upgrade` are no longer silent (INT-5186, item 6). On 2026-09-10 three 0.7.2 bridges ran next to an installed 0.7.6 and nothing but `auto-status` said so. Each bridge now records its current conversation (`conversationId`), its `codexHome`, and its pool (`accounts`) in `status.json`; `xswap list` marks a running session whose `bridgeVersion` differs from the installed xswap under Running sessions — `⚠ bridge 0.7.8 · reopen with codex resume UUID to load 0.8.0` (`xswap run -- resume UUID` when the `codex` command is not connected; `xswap run --auto --accounts … -- resume UUID` when automatic defaults are off). A conversation is only named once it is saved; otherwise, and for records written before this release, the line reads `exit and reopen it`; desktop sessions read `quit and reopen it with xswap app`. `xswap doctor`'s `auto cli-runs` row turns WARN with the same lines (still read-only, exit code unchanged), and `xswap upgrade` prints them after a successful install, reading the records before the reinstall. Nothing is stopped or restarted; exit the old session before resuming, since it still holds the conversation's writer lock. The per-session lines from `upgrade` first appear when upgrading from 0.8.0 onwards (the upgrading process runs the previous code).
- **A login the usage service rejects is now remembered, not just shown once** (INT-5186, item 8). A quota read answered with a 401-class error (`sign in again to read usage`) used to be visible only in that one live `xswap list`: `--cached`/`--offline` views, `run --best`/`use --best`, and the automatic-switching pool kept treating the account as a candidate, and `doctor` only looked at the local token's expiry. xswap now records the rejection in `auth-state.json` (0600; account name, local login label, time, short reason — never tokens). While the record stands: `list`/`usage` with `--cached` or `--offline` show `sign-in required · xswap login NAME` without spawning Codex; `--best` and the pool skip the account without probing it, and a skipped candidate's reason is written to the bridge status record (`xswap auto-status` shows it); a bridged session whose current account was rejected moves to another pool member before its next idle turn, and a new auto session starts on the first pool member not recorded as rejected (it stops with `sign-in required; run: xswap login NAME` when every member is); `xswap use NAME`/`switch` refuse the account and a pass-through `codex exec` falls back to the caller's own home with that reason; `xswap doctor` adds a `NAME: sign-in` row (FAIL, WARN when the account is disabled). `xswap login NAME` and `xswap add NAME` clear the record, and so does any later successful live fetch (`xswap list`/`usage` without `--cached`, the menu bar's refresh). Like the usage cache, the record only counts while its saved label matches the account's current login.
- A bridge no longer takes its own word for which account a session runs on. After every `account/login/start` it sends (session start, a proactive or quota-error switch, `xswap switch`, an `xswap login` re-login), it reads the login back from its app server (`account/read`; local, no token refresh) and records the answer as `verifiedAccount`, `verifiedIdentity`, and `verifiedAt` next to the self-reported `account` in `auto/**/status.json` and `xswap auto-status`. A server that holds no login or another one fails the switch with reason `identity mismatch`, gets the previous account's login re-sent, and the session stays on it; at session start the bridge stops with that reason instead of opening on an unknown account. When the server cannot answer (a Codex CLI without `account/read`, a timeout, a token without an email claim) the switch is kept and `verifyReason` says why. `xswap doctor` gained an `auto sessions` row that warns about running sessions whose login was never confirmed or whose account has since been signed in as someone else. Plan type is never used as identity. Background: the 2026-09-10 diagnostics showed a failed session whose record still named the account it had asked for, and codex-cli 0.154.0 keeps the previous login installed when an external-token login is rejected.
- `xswap auto-tick [--dry-run] [--cached SECONDS] [--json]` is the between-sessions counterpart of the bridge's before-turn check. Only a running bridge could move off an exhausted account; a new session or `codex exec` started on whatever `xswap use` last selected. The tick reads the pool and weekly reserve from automatic switching, fetches quota like `xswap list` (sharing its cache), and when the selected account is at or below the reserve -- or its `codex` limit is reached, or its login needs a sign-in -- selects the pool account with the most headroom **above** the reserve through the `xswap use NAME` path: registry default, pool order, and the manual-switch broadcast to running bridges. Exit codes follow cswap's `auto --once`: `0` switched, `1` error, `2` no action, `3` blocked. Unknown quota is never a reason to switch; disabled and sign-in-required accounts are never targets; a manual `xswap use` that lands during the fetch wins. No cooldown: the rule only compares the selected account with the reserve, and weekly remaining only rises at its reset, so a switch cannot reverse until then.
- `xswap alert --install --auto-switch` runs `xswap auto-tick --cached SECONDS` before the `list --warn` step of the launchd job and posts a notification (title "xswap auto-switch") per `switched:` line; `alert --status` reports `auto-switch: on|off`, `--uninstall` says the step went with it, and non-macOS prints the two-step crontab line. `Manager.best_account` now delegates to the module-level `rank_candidates(rows, model, exclude, weekly_remaining)`; `use --best`/`run --best` behave as before.
- Tests: `test_wrapper.py` covers the codex-entry reconnect directly (`wrapper_drift`/`reconnect_wrapper`: connected, replaced, dangling, non-executable, not user-owned, alias-to-proxy, relative target, regular file, missing entry, half-written record, auto-disabled; record-then-swap with no temp link left; quiet second call; swap failure message; corrupt settings), the reconnect on bridged-session exit even when the bridge fails, the `xswap-codex` entry point's exec path, the PATH lookup behind every wrapper check, and run-record pruning/counting. 0.7.8 shipped with that path tested only through `auto-enable`.

## 0.7.8

A Codex update no longer disconnects plain `codex` from xswap (INT-5121). Codex's standalone updater (ctrl+u in the TUI, `codex upgrade`, the install script) re-points the user-owned `~/.local/bin/codex` symlink at its new release, silently undoing `--wrap-codex`: the next plain `codex` ran the new binary against `~/.codex` and its own account, and only `xswap doctor` noticed (2026-09-10 on a 0.153.4 → 0.154.0 update, the session hit that account's exhausted weekly limit).

- While automatic switching is enabled, every `xswap` launch, `xswap list`/`usage` (including the `alert` job), and `xswap use`/`switch` checks the wrapped entry; if a Codex update replaced it with another executable, xswap records that release as the real Codex (`realCodex`/`originalTarget` in `auto.json`) and points the entry back at `xswap-codex`, printing one line. A bridged CLI session also reconnects it when it ends, since ctrl+u runs the updater inside that session.
- Dangling or non-executable targets and a disabled auto mode are left alone; `xswap auto-disable` now restores the entry to the release the update installed rather than the one wrapped originally.
- `xswap doctor`'s wrapper row says the gap closes on the next launch, list, or use, and still names the manual `auto-enable --wrap-codex` command.

## 0.7.7

A re-login now takes effect everywhere at once (INT-5099). Previously `xswap login NAME` only ran `codex login`: the quota cache kept the pre-login numbers for `list --cached`/the alert job (a same-email re-login looked like a cache hit), a bridged session on that account kept the old tokens until Codex itself asked for a refresh, and the menu bar showed the old state for up to five minutes.

- `xswap login NAME` (and the first sign-in through `xswap add`) drops the account's cached quota, reads it live once, and prints the weekly line (`NAME weekly: 43% used · 57% left · resets in …`) or the short failure reason. The sign-in itself is reported either way.
- Running bridged sessions **already on NAME** re-authenticate in place through the manual-switch channel (`account/login/start` with the new tokens, quota republished to the TUI, `switches` unchanged). Sessions on other accounts are left alone; they re-read the home the next time they consider it. `switch_running()` gained `only_current=`.
- The menu bar app watches the xswap root (usage cache, registry, auto settings) and refreshes within a second of a login or `use` instead of waiting for the 5-minute timer. Its own refresh writes are absorbed, so no refresh loop.

## 0.7.6

Audit follow-up after the 2026-09-09 account-switch incident (INT-5079/INT-5085). Display, guidance, and metadata only; no change to account, session, or automatic-switching behavior.

- **`xswap use`/`switch` could silently never reach running sessions.** A CLI launch created the `auto` control directory through `mkdir(parents=True)`, so it took the umask (0755) unless a desktop session or a runtime-home launch had created it first; `switch_running` then treated it as unsafe and returned an empty report. Every level of `auto/cli-runs/<run>` is now created 0700 (an existing `auto` is repaired on the next launch), an unsafe directory is reported by `use` with the `chmod 700` fix and a non-zero exit, and `doctor` has an `auto control dir` row.
- Bridge status records carried a hard-coded `bridgeVersion` of `0.7.2`; they now report the installed version, and `quotaKnown` persists across every status write instead of vanishing after the first `ready` (it showed as `null` in `auto-status`).
- `xswap list`/`usage` name the fix instead of a bare "unavailable": a login the usage service rejects reads `sign-in required · xswap login NAME`, other failures keep their short reason (`unavailable · …`).
- `xswap list` warns when automatic switching is on but no other pool account has weekly quota above the reserve, so an exhausted fallback is visible before it matters.
- `xswap use`/`switch` no longer print a zero-filled bridge counter; with no live bridges they say so and name the account new sessions start as. Exit codes are unchanged.
- `xswap doctor`'s wrapper row names the reconnect command when the `codex` link was replaced outside xswap (what a Codex update does).
- `xswap upgrade` reports how many running bridged sessions still use the previous code and that reopening them loads the new tag.
- README install/upgrade snippets had pinned `v0.6.1` since that release; they now pin the current tag and a unit test fails when they drift from `__version__`.
- Lint: `ruff` (F, E9, B, UP) runs in CI; unused imports, a shadowed `setUp`, and `asyncio.TimeoutError` aliases were cleaned up.

## 0.7.5

- `xswap use` / `switch` now say when the ordinary `codex` command is not connected to xswap: plain `codex` keeps using its own home (`~/.codex` unless `CODEX_HOME` is set) and the account signed in there, and the selection applies only to `xswap` and `xswap app`. The message names that home, its local account label, and the `xswap auto-enable --accounts … --wrap-codex` command that connects it. Nothing is printed when the wrapper is connected or when the selection already is that home.
- `xswap doctor` reports the `wrapper` row as WARN instead of OK when two or more accounts are registered and the `codex` command is not connected.
- Background: a running Codex CLI (0.153.4) does not re-read `auth.json`, so a plain session cannot change accounts without restarting; only sessions started through xswap (or the connected `codex` command) switch in place. See the "Already-running ordinary sessions" row under Automatic switching.

## 0.7.4

- Keep automatic CLI sessions open when the usage service is unreachable at startup. Credentials are still validated; quota is re-read before the first turn (`quotaKnown: false` in `auto-status` until then). Previously the bridge exited and Codex showed only `remote app server ... closed during initialize`.
- Record a short, non-secret `reason` on `stopped` bridge records and print it in the disconnect message after the TUI exits.

## 0.7.3

- Show account emails, weekly remaining-quota gauges, local reset times, and detected running sessions in a compact account tree. The default list omits short quota windows; `--details` includes other windows and reset credits.
- Keep list positions in registration order and accept `xswap switch NUMBER`. Account names remain supported; numeric names can be selected with `xswap use NAME`. Removing an account renumbers subsequent slots.
- Default `xswap list` to English regardless of locale; `--lang ko` remains available.

## 0.7.2

- Keep account-switch status logs out of the active Codex TUI; status remains available through `xswap auto-status`.
- Apply `switch`/`use` selections to the enabled automatic-mode pool so new CLI/app sessions start with the selected account, including accounts outside the previous pool.
- Omit conversations held by other writers from resume lists and reject explicit locked UUID resumes before TUI startup, without deleting locks or interrupting their owners.
- Only print reconnect hints for saved conversation IDs. Reopen existing CLI sessions once after updating to load the new bridge.

## 0.7.1

- Fix the in-session `/resume` picker in automatic CLI mode by sharing the running app server through isolated browsing connections. Preserve the main TUI, conversation, and account switching.
- Show earned reset counts and available expiry dates in `list` and `usage`, including JSON and cached results. Missing availability remains unknown; reading usage never consumes a reset.
- Existing CLI sessions need to be reopened once to load the updated bridge.

## 0.7.0

Onboarding, unattended-operation safety, and shell ergonomics. No change to existing account, session, or auto-mode behavior unless a new command or flag is used; the only default that changes is the dashboard language (see below).

- `xswap init [--yes] [--no-auto] [--weekly-remaining PCT]` walks first-run setup: registers the existing Codex login as `main`, offers to add accounts, enables automatic switching with the `codex` wrapper when two or more accounts exist, sets the weekly reserve, and finishes with `doctor`. Idempotent; commands run against an empty registry now hint at it (#29).
- `xswap doctor` reports a per-account `openclaw cooldown` row: FAIL when OpenClaw holds a profile in cooldown while the usage cache shows weekly quota remaining, WARN when remaining is unknown, omitted when OpenClaw or its state database is absent. `xswap openclaw --clear-cooldown [NAME | --pool a,b] [--dry-run] [--yes]` stops the Gateway, takes a private WAL-aware sqlite backup, removes only the stale block, and restarts the Gateway; it never writes if the Gateway stop fails (#31).
- `xswap alert --install [--warn PCT] [--every MINUTES] [--cached SECONDS]` writes a private wrapper and a `launchd` agent that runs `list --warn` on a schedule and posts a macOS notification per warning line; `--uninstall` and `--status` manage it; non-macOS prints the equivalent crontab line. All install-time paths are shell-quoted (#27).
- `xswap list`, `usage`, and `dashboard` print English by default; `--lang ko` or `XSWAP_LANG=ko` (or a Korean `LC_ALL`/`LANG`) keeps the previous Korean text verbatim. The menu bar app resolves the same rule and passes it to its dashboard subprocess. `--json`, `--short`, and `--warn` output are unchanged (#28).
- `xswap completion zsh|bash` prints a completion script generated from the parser; account-name positions complete from the local registry with `list --offline --json`, never touching the network (#30).
- Packaging: a unit test now fails when a top-level module is missing from `py-modules`, after two of this release's modules were initially left out (#29).
- GitHub Actions bumped: checkout 7.0.1, setup-python 7.0.0, setup-node 7.0.0 (#1, #2, #3).

## 0.6.3

- `xswap --version` reported 0.6.1 on the 0.6.2 release because the version string is kept in both `pyproject.toml` and `codex_swap.__version__` and only one was bumped. Both now read 0.6.3, and a unit test fails whenever the two drift. `xswap upgrade` compares `__version__` with the newest tag, so a stale string could also make it skip a real update.

## 0.6.2

- `xswap list --short` / `usage --short` place windows by duration (`windowMinutes`), not by `primary`/`secondary` position. Live servers can report only a seven-day window and still call it `primary`; that value now lands in the `7d` slot instead of the `5h` slot. Position remains the fallback when the duration is absent.

## 0.6.1

- Print a shell-quoted xswap resume command after the CLI closes its temporary bridge. Preserve the original home, account store, current account, and pool; do not reuse the closed Unix socket.

## 0.6.0

Account lifecycle, quota-aware selection, unattended failover, and operations tooling. No change to existing account, session, or auto-mode behavior unless a new flag is used.

- `xswap login NAME [--device-auth]` re-authenticates a registered account in place; `add` now points to it instead of refusing (#5).
- `xswap remove NAME [--purge] [--yes]` drops an account from the registry; managed profile files are kept unless `--purge`, registered homes are never deleted, pooled or running auto-mode accounts are refused; the account's usage-cache entry is dropped too (#16).
- `xswap disable NAME` / `enable NAME` hold an account out of `use`, `--auto` pools, `openclaw`, and live usage fetches without deleting it (#8).
- `xswap add NAME --use` selects the account right after login; the first account added is selected automatically (#11).
- `xswap run --best [--model M] -- ...` launches the account with the most remaining quota, chosen once before launch; covers `codex exec`, which the live bridge cannot (#14, #19).
- `xswap use --best [--model M] [--openclaw]` (also `switch --best`) selects that account as the default and fails instead of keeping the current selection when no quota is known (#20).
- `xswap openclaw --pool a,b,c [--allow-mixed]` records every pooled account and passes the full order to OpenClaw so it rotates on its own cooldowns; accounts from different ChatGPT organizations, or whose organization cannot be verified, are refused unless `--allow-mixed`; the single-account path is unchanged (#10).
- `xswap list --short` prints a one-line `name 5h/7d` summary for status lines; `xswap list --warn PCT` exits 3 when any window is below the threshold; `--cached SECONDS` reuses a private 0600 cache of whitelisted quota fields for `list`, `usage`, `run --best`, and `use --best` (default remains uncached) (#13, #15, #21).
- `xswap map NAME [PATH]` / `unmap` choose a default account per directory for bare `xswap`, `run`, `app`, `status`, and `usage`; `openclaw` never follows a mapping (#17).
- `xswap doctor [--json]` reports codex, credential store, per-account token expiry and plugin state, auto-mode pool, and OpenClaw SDK availability read-only; disabled accounts never fail the run (#18).
- `xswap upgrade [--tag]` reinstalls the latest release tag with uv (#6).
- `xswap auto-status [--prune]` removes CLI run records that are not running and older than 7 days (or any not running with `--prune`); records younger than 60 seconds are never removed (#7).

## 0.5.1

- Resume/fork explicit session UUIDs from their original Codex home while retaining the automatic authentication bridge; preserve the original conversation instead of copying it.

- Deliver `switch` / `use` selections to running compatible bridges, defer changes while turns are active, and report acknowledgements. Keep the same server and thread; add `--default-only` for selection without delivery.

## 0.5.0

- Weekly-first text dashboard with colored usage bars, selected-first sorting, private defaults, and explicit details.
- Native macOS menu app built from bundled Swift source, five-minute refresh, stale/error status, and running bridge account labels.

## 0.4.3

- Hide Spark quotas in default list/usage text output; use --include-spark to show them. JSON quota data and automatic account selection are unchanged.

## 0.4.2

Version 0.4.1 was skipped: its tag accidentally points to the unchanged 0.4.0 source. Use 0.4.2 for these fixes.

- Reject unsafe credential ownership, permission modes, symlinks and non-regular files before registration, automatic refresh, and OpenClaw sync. Read validated descriptors without modifying source files.
- Pin the security scanner digest and pip-audit dependency hashes.

- Prepare the public open-source release with MIT licensing, English/Korean documentation, HTTPS installation, and contribution/security guidance.
- Replace personal account labels in examples with generic labels and document what diagnostics can expose.
- Harden CI action references and add dependency/security maintenance configuration.
- Keep existing account and session behavior; automatic switching still requires an auto-mode session.

# 0.3.2 — 2026-09-06

- Add configurable weekly reserve switching with `auto-policy --weekly-remaining 10`.
- Reload policy before idle turns in desktop and CLI bridges; require candidates above the same threshold to avoid account churn.
- Preserve active turns, exhausted-window checks, and current-account use when no candidate meets the reserve.
- Expose runtime bridge version/threshold and test proactive failover with real Codex server and TUI fixtures.

# 0.3.1 — 2026-09-06

- Keep plugin caches physically inside each account/auto CODEX_HOME so browser trusted RPC realpath checks pass without expanding trust.
- Add atomic `repair-plugins` migration for legacy shared links; retain active processes, source caches, credentials and conversations.
- Add actual trusted-runtime rejection/recovery tests, including cached-failure recovery via supported js_reset and browser setup before/after desktop-server and CLI account failover.

# 0.3.0 — 2026-09-06

- Add opt-in `xswap app --auto --accounts first,second` and `xswap run --auto --accounts first,second` for desktop and interactive CLI account failover without restarting its server or changing thread IDs.
- Preserve failed turns and continue in a new turn after structured usage-limit errors; stop on exhaustion, cancellation, and unsupported authentication.
- Add reversible `xswap auto-enable --wrap-codex` / `auto-disable` defaults and a private Unix WebSocket CLI transport.
- Add `xswap auto-status`, bounded retries, explicit account pools, source-token refresh, and isolated runtime storage.
- Validate with unit tests and real Codex binaries against a local fixture backend. Existing ordinary processes cannot be retrofitted; noninteractive codex exec retains its original authentication.

# Changelog

## 0.2.0

- `xswap list`에 실시간 잔여 사용량·초기화 시각·남은 시간 표시.
- `xswap usage [계정]`, `list --offline`, `list/usage --json` 추가.
- Codex 기본 한도와 모델별 추가 한도를 구분하고 조회 실패·미로그인·API 키 상태를 명시.
- 공식 App Server 조회만 사용하며 모델 턴 생성이나 계정 전환 없이 동작.

## 0.1.0

- Codex 계정 등록·추가·선택과 CLI 실행.
- macOS에서 계정별 데스크톱 프로필 실행.
- `xswap openclaw`와 `xswap use --openclaw`: 대상 에이전트 인증 선택, 소규모 백업, Gateway 인증 재적용.
- 오래된 토큰 덮어쓰기 방지, 부분 실패 보고, 인증값 비출력.
- `uv` 설치 패키지와 macOS/Linux CI.
