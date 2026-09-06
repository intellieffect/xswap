# Contributing

Small, reproducible changes are welcome. Use generic account labels and fixture credentials. Please read [SECURITY.md](SECURITY.md) before attaching diagnostics.

## Development

Requires Python 3.11+, uv, and Node 22+ for OpenClaw fixture tests. Ordinary unit tests do not require a Codex login or a running browser/Gateway.

```sh
git clone https://github.com/intellieffect/xswap.git
cd xswap
uv sync
uv run python -m unittest discover -v
node --test tests/*.test.mjs
uv build
```

Do not run mutating launcher commands against your usual account home just to test a change. Use temporary homes or `CODEX_SWAP_HOME`; preserve unrelated processes and user data.

## Optional integration checks

These require your own installed Codex binary. They use a local fixture backend and fake credentials, not real quota exhaustion. Inspect their scope before execution.

```sh
uv run python tests/live_codex_smoke.py
uv run python tests/live_cli_smoke.py
XSWAP_TEST_RESERVE=1 uv run python tests/live_cli_smoke.py
XSWAP_TEST_PICKER=1 uv run python tests/live_cli_smoke.py
```

`XSWAP_SMOKE_CODEX=/absolute/path/to/codex` selects a binary. The CLI smoke test requires a PTY. macOS browser-runtime checks additionally require the locally installed app and browser plugin:

```sh
uv run python tests/plugin_runtime_smoke.py
XSWAP_TEST_BROWSER=1 XSWAP_TEST_RESERVE=1 uv run python tests/live_cli_smoke.py
```

These setup checks do not validate a live browser backend or page navigation. Tests must never redistribute provider binaries/plugins or use actual credentials as fixtures.

## Pull requests

Describe the concrete problem, changed behavior, relevant tests, and compatibility limits. Keep changes focused and preserve existing account/session behavior. Code that handles auth must redact raw payloads and preserve private file permissions. New protocol behavior needs a meaningful regression test; document unsupported surfaces explicitly.

By contributing, you agree to license your contribution under this repository's MIT license. Do not submit code or data you do not have permission to contribute.
