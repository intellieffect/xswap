"""Everything xswap knows about Codex CLI, the Codex desktop app and OpenClaw.

Deliberately empty of imports: `xswap.providers.codex.<module>` is imported
directly (and aliased back to the old `xswap.<module>` names), so a package
body that pulled submodules in eagerly would make every one of them a cycle
with `xswap.manager`. `CodexProvider` lives in `.provider`.
"""
