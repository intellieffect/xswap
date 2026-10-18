# Codex profile and hook definition supply Implementation Plan

**Goal:** Supply missing Codex profile files and approved hook definitions to managed account and automatic runtime homes without changing user-owned files or trust state.

**Architecture:** Keep the four existing shared tooling entries unchanged. Add a Codex-only helper for missing-only definition symlinks, refresh managed account homes from `manager.source`, then populate automatic homes from the selected account home to preserve overrides. External registered homes and read-only/dry-run paths receive no refresh.

**Tech Stack:** Python 3.11+, pathlib, pytest, existing Windows `win auto` runner.

Execution is inline in the assigned worktree and branch; worker instructions prohibit sub-workers. No push, PR, installation patch, actual home edit, or new backup is authorized.

## Authorized continuation (2026-10-18, ctx_c5b76ef5b038)

The earlier no-push/PR and runner-approval statements below are historical and superseded by the current dispatch. User instruction (verbatim): "기존 계획의 승인대기/PR 금지는 최신 사용자 승인으로 해소됨." User instruction (verbatim): "반복 승인 요청 없이 기존 승인된 Git Windows 경로를 구현 선택으로 사용."

Existing `win_snapshot.snapshot(project_override="xswap")` and `transfer(pin=True)` succeeded with unchanged safety checks; no runner registration or dotfiles edits were needed. The existing assigned worktree and test WIP were retained. Production edits include registry, the two launchers, a Codex tooling helper and the provider refresh contract. Lead approved provider.py/base.py and the fake provider extension after the architecture regression demonstrated core must remain platform-neutral. Read-only lookup and dry-run return before refresh. Definition symlinks intentionally follow source updates; trust remains per-home and is never copied or approved by xswap.

Validation commands, raw results, preservation limits and final artifact details are recorded in `docs/superpowers/plans/2026-10-18-codex-profile-hooks-verification.md`.

## Evidence and gates

Raw code evidence (2026-10-18): `src/xswap/core/registry.py:120` returns an existing account immediately; line 126 supplies only `("config.toml", "AGENTS.md", "skills", "rules")`. The same fixed list occurs in `src/xswap/providers/codex/codex_cli.py:595` and `src/xswap/providers/codex/launcher.py:221`.

Audit source: `/Volumes/BIGNO-FC2T/Obsidian/Bigno/Research/2026-10-18-dotfiles-official-audit/report.md:144-145`:

> “Config profile files live next to config.toml as $CODEX_HOME/profile-name.config.toml”
> “Non-managed hooks must be reviewed and trusted before they run.”
> “Codex records trust against the hook’s current hash”

Summary: profile discovery is missing at provisioning and launch boundaries; hook definition availability does not authorize trust approval. Index: implementation tasks below map the missing boundaries and preservation requirements to tests.

- [x] Receive upstream confirmation about Orca hook injection/ownership before implementing hook supply: dotfiles Lead `ctx_4b819014e203`, message `msg_facea56e3ffb` (2026-10-18T03:52:11Z), agreed to missing-only supply while preserving existing files, links and trust; dotfiles owns actual-home application and hooks/list verification.
- [ ] Resolve runner registration through the coordinator, without editing dotfiles or creating a new transfer route.

Coordinator question `msg_75c7f22c98c9` remains pending user decision: adding xswap to the shared Windows runner is outside this supply-only change. Worker `ctx_f4a1da778152` settled failed and was released after preserving this plan and unverified test WIP; resume implementation in a fresh Dispatch after runner authorization. No production changes or commits exist yet.

Observed runner failure: `win auto -- python -m pytest tests/test_codex_swap.py -q` exits 1 before pytest: `win: Project is unknown or ambiguous; configure project aliases or project_override.` Log: `/tmp/xswap-profile-hooks-baseline.log`. `win status` exits 0; log `/tmp/xswap-profile-hooks-win-status.log`. No local fallback.

## Task 1: Reproduce account provisioning gaps

**Create:** `tests/test_codex_tooling.py`.

- [ ] Use `tmp_path` for source, root, and external homes; construct `Manager(root, source)` exactly as existing `AccountTests` do.
- [ ] Add failing tests for a new managed account and an existing account receiving a late-added `audit.config.toml`:

```python
def test_existing_account_gets_late_profile(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    manager = Manager(tmp_path / "store", source)
    home = manager.prepare("work")
    profile = source / "audit.config.toml"
    profile.write_text('model = "example"\n')
    assert manager.prepare("work") == home
    assert (home / profile.name).is_symlink()
    assert (home / profile.name).resolve() == profile
```

- [ ] Test repeated prepare, existing regular files, valid symlinks, broken symlinks, directories, nonmatching extensions, broken/directory source entries, source=destination, and concurrent `FileExistsError`.
- [ ] Run `win auto -- python -m pytest tests/test_codex_tooling.py -q`; verify failures identify missing profile supply before production changes.

## Task 2: Implement Codex-only definition supply

**Create:** `src/xswap/providers/codex/tooling.py`.

**Modify:** `src/xswap/providers/codex/provider.py`, `src/xswap/core/registry.py`.

- [ ] Add `supply_tooling(source: Path, home: Path) -> None`: return for same resolved directory; inspect only immediate `*.config.toml` (and `hooks.json` after gate); accept entries whose targets are regular files so source symlinks from managed homes work; skip broken links and directories; use absolute symlink targets.
- [ ] Preserve every existing destination including dangling links with `dst.exists() or dst.is_symlink()`; create with `dst.symlink_to(src)` under `contextlib.suppress(FileExistsError)`. Never merge, copy, back up, parse hook trust, or scan arbitrary extensions.
- [ ] Add `refresh_managed_tooling(manager, home, accounts=None)`: require Codex, an explicit managed registry record, and both lexical/resolved containment in the managed root. Do not create a missing external directory or follow a managed-directory symlink out of the root.
- [ ] Call supply in Codex `prepare_home` for new accounts. In existing-account `Registry.prepare`, call the Codex-only refresh guarded by provider identity, using already-read account records inside the lock; leave other providers' lifecycle untouched.
- [ ] Run the targeted suite and confirm the tests now pass.

## Task 3: Actual launch and auto-home refresh

**Modify:** `src/xswap/providers/codex/launcher.py`, `src/xswap/providers/codex/codex_cli.py`.

**Test:** `tests/test_codex_tooling.py` plus existing launch/race regression tests.

- [ ] Refresh existing managed homes in normal CLI/app launches after their dry-run returns and before launching the subprocess.
- [ ] For auto CLI and desktop, refresh the selected managed source account first, then call `supply_tooling(selected_home, auto_home)` alongside the existing four-entry loop. Retain selected-account override precedence; never refresh external accounts from manager.source.
- [ ] Check resumed sessions using an original managed home also receive refresh while external resume homes stay untouched.
- [ ] Mock process launch/AccountPool only at external boundaries; assert profile availability at the launch call for normal CLI, normal desktop, auto CLI, and auto desktop.
- [ ] Snapshot dry-run and account-read trees before/after; assert no changes. Test unrelated provider preparation/launch retains its current behavior.

## Task 4: Preservation and final verification

**Test:** `tests/test_codex_tooling.py`.

- [ ] After hook ownership confirmation, add tests for hook definition supply and source definition replacement while destination trust hash/state bytes remain identical.
- [ ] Seed auth, plugins, memories, sessions, config trust state and unrelated files; compare bytes and link targets before/after helper and managed refresh calls. Check no backup paths are created.
- [ ] Run `win auto -- python -m pytest tests/test_codex_tooling.py tests/test_codex_swap.py -q`, then `win auto -- python -m pytest -q`; capture stdout/stderr in temporary logs and record actual exit codes. Do not claim success if the runner cannot start or tests do not finish.
- [ ] Review `git diff --check` and scoped changes. Commit only after verified tests; otherwise report the exact unverified files and blocker to the coordinator.
- [ ] Report modified paths, command/exit/log, remaining risks, commit, Aside opened/closed 0/0, and new backups 0. Send worker_done exactly once.
