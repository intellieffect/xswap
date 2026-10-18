# Codex profile/hooks supply verification — 2026-10-18

## Raw evidence

Task `task_e1ec52a80d40`, dispatch `ctx_c5b76ef5b038`, assigned branch `fix/codex-profile-hooks-supply`. Host `bignos-Mac-Studio-2.local`; all tests and builds run through `win exec` on Windows WSL Ubuntu. Mac only reads/edits files and transfers Git objects.

Existing Windows route, invoked from the assigned Mac worktree (stdout/stderr logged):

```python
import sys, json
sys.path.insert(0, '/Users/bigno/dotfiles/scripts/windows-runner')
import win_snapshot
print(json.dumps(win_snapshot.transfer(
    win_snapshot.snapshot('.', project_override='xswap'), pin=True), default=str))
```

This is the existing safety-checked snapshot/Git transfer API, not a new transfer implementation. Reference: dotfiles PR189 commit `6d29023008c9199ddb7d883a012163368305ccc7`, `docs/dotfiles/2026-10-18-verification.md:70-83`. No safety-check edits/bypass, global runner registration, source scp, or dotfiles changes. Runner-owned immutable Windows pins are test fixtures, not extra developer worktrees or backups.

Windows pin base: `/home/yjh99/Projects/.win/pins/xswap-4e61901cfb8d7b7b27c20b54/`.

| Stage | Pin SHA | Raw command after `cd` into pin, via `win exec -- bash -lc` | Exit/result | Mac log |
|---|---|---|---|---|
| Existing WIP RED | `693905b5160edffc75b2ee53cde4b37a44736122` | `PYTHONPATH=src python3 -m pytest tests/test_codex_tooling.py -q` | 1; 2 failed, 7 passed | `/tmp/xswap-red.log` |
| Initial targeted regression | `a6b8e3d881d024c50fe82d58f77d80125fed74a2` | `PYTHONPATH=src python3 -m pytest tests/test_codex_tooling.py tests/test_codex_swap.py tests/test_cli.py tests/test_wrapper.py tests/test_relocate.py -q` | 0; 371 passed, 17 subtests passed | `/tmp/xswap-targeted.log` |
| Architecture regression caught | `be9fbfa62fb43d84a434ed30c2930dd528048e0b` | `PYTHONPATH=src python3 -m pytest -q` | 1; 1 failed, 957 passed, 2 skipped, 104 subtests passed | `/tmp/xswap-full.log` |
| Lint | `be9fbfa62fb43d84a434ed30c2930dd528048e0b` | `uvx --from ruff==0.16.6 ruff check .` | 0; All checks passed | `/tmp/xswap-quality.log` |
| Typecheck with runtime dependency | `be9fbfa62fb43d84a434ed30c2930dd528048e0b` | `uv run --no-project --with pyright==1.1.407 --with "websockets>=15,<18" pyright` | 0; 0 errors, 0 warnings, 0 informations | `/tmp/xswap-types.log` |

Initial `python3 -m ruff` failed because ruff was not installed in system Python. `uvx` supplied the pinned tool. Isolated `uvx pyright` initially lacked the declared websockets runtime dependency; the explicit `uv run --with` command above resolved the environment without changing project dependencies. The architecture failure identified forbidden provider-specific imports in core; the test was retained unchanged.

## Summary and preservation boundaries

- Supply only immediate root `*.config.toml` and `hooks.json` whose targets are regular files; absolute missing-only symlinks preserve source updates.
- Preserve existing files, directories, valid links, dangling links and account-specific additional definitions. Concurrent destination creation is harmless.
- Refresh only explicitly managed Codex accounts within the managed profile tree; do not refresh externally registered or escaped homes from the root source. Automatic homes inherit the selected account's definitions after managed refresh, preserving overrides.
- Read-only account lookup and dry-run launch paths do not refresh. Normal CLI/app launches, auto CLI/app launches and managed original-home resumes refresh at actual launch boundaries.
- No hook trust hashes, approval state, authentication, memory, sessions or plugin state are supplied by this helper. Existing four shared entries and existing plugin/package lifecycle remain unchanged. Hook execution still requires Codex's independent trust checks; no actual-user-home install or hooks/list runtime verification was performed.
- All new behavior tests use temporary homes and mock process launch boundaries. Desktop paths are tested on Windows with platform/process mocks, not live macOS GUI or paid model calls.
- Existing plan/test WIP retained. New backups 0; local worktrees/stashes/clones/archives 0; Aside opened/closed 0/0. No service/session restart, deployment, installation or merge.

## Index

Implementation: account registry, Codex tooling helper, CLI and desktop launchers. Regression: `tests/test_codex_tooling.py` plus unchanged existing suites. Historical plan: `2026-10-18-codex-profile-hooks-supply.md`. Final commit, PR and wheel artifact are reported to the Lead; merging and actual-home installation remain the Lead's work.

## Final verified source

Snapshot `61b1a9bd64f1f393b4df56cc940ff1ad091df972` includes the approved provider.py/base.py/fake-provider contract correction. Lead's exact reply is preserved in `/tmp/xswap-scope-reply.log`; the import boundary test/allowlist were not edited.

```sh
win exec -- bash -lc 'cd /home/yjh99/Projects/.win/pins/xswap-4e61901cfb8d7b7b27c20b54/61b1a9bd64f1f393b4df56cc940ff1ad091df972 && PYTHONPATH=src python3 -m pytest -q'
# exit 0: 958 passed, 2 skipped, 104 subtests passed in 81.12s (0:01:21)
# stdout/stderr: /tmp/xswap-final-tests.log

win exec -- bash -lc 'cd /home/yjh99/Projects/.win/pins/xswap-4e61901cfb8d7b7b27c20b54/61b1a9bd64f1f393b4df56cc940ff1ad091df972 && uvx --from ruff==0.16.6 ruff check . && uv run --no-project --with pyright==1.1.407 --with "websockets>=15,<18" pyright'
# exit 0: All checks passed! / 0 errors, 0 warnings, 0 informations
# stdout/stderr: /tmp/xswap-final-quality.log
```

`git diff --check` exits 0. Only this verification documentation is updated after the final tests; source and tests are unchanged. The committed tree is re-snapshotted through the same API for the wheel build, with the returned `head` and `head_matches` verifying provenance. The wheel receipt (commit, pin, Windows Downloads path, SHA256 and build exit code) is attached to the PR and worker completion; local build log `/tmp/xswap-wheel.log`.

Build reproduction after merge: use a checkout at the intended exact Git commit, run the same snapshot/transfer API, then on its returned Windows `pin_root` run `uv build --wheel --out-dir /mnt/c/Users/yjh99/Downloads/xswap_wheel_20261018-1400` and `sha256sum /mnt/c/Users/yjh99/Downloads/xswap_wheel_20261018-1400/*.whl`. Use a new timestamp for a later build. Transfer only the final wheel if needed; installation is not part of this worker task.
