# Changelog

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
