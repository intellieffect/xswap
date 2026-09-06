# Security and privacy

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/intellieffect/xswap/security/advisories/new). Do not open a public issue containing credentials, private account identifiers, session transcripts, or an exploit that exposes them. If private reporting is unavailable, open a public issue asking for a private contact **without sensitive details**. There is no guaranteed response-time SLA.

Only the latest release receives security fixes. Automatic switching is experimental; this policy is not a claim of an independent security audit.

## Data boundaries

- xswap is a local launcher and auth bridge, not an authentication provider. Users authorize their own accounts through Codex.
- Source Codex `auth.json` files and OpenClaw backups contain usable credentials. Treat copies as secrets; file permissions do not encrypt them.
- Auto-mode tokens pass in memory over a local child-process protocol. CLI transport uses a random private directory and Unix WebSocket, not a TCP listener.
- xswap's operational status excludes token/prompt bodies, but account labels, paths, PIDs, quotas, errors, and dry-run output can identify users or machines. Redact these before sharing. Codex and provider services have their own storage/telemetry behavior.
- Auto-mode accounts share task context and local tool permissions. Account separation is not a sandbox. Do not combine unrelated users or organizations.
- Plugin code is trusted executable code. xswap materializes local caches without relaxing trusted roots; it does not audit third-party plugins.
- Explicit OpenClaw sync writes credentials and creates sensitive recovery backups. It is distinct from auto-mode token handling.
- `xswap list`/`usage`/`run --best`/`use --best --cached SECONDS` (opt-in; default is uncached) persists `usage-cache.json` (mode `0600`) per account, holding the same whitelisted fields shown on screen for that account: remaining percentages, reset times, plan type, credits, and the local account label — which can be an email address. It never stores raw server responses or tokens, a failed lookup is never cached, and a cache entry is discarded (not reused) once the account's current login label no longer matches the one it was saved under.

## Files that must not be submitted

Do not attach `auth.json`, `accounts.json`, `auto.json`, profile folders, `.env` files, private keys, OpenClaw auth backups, SQLite databases, raw session JSONL, browser data, or unredacted API output. Never paste an actual token into a test; fixtures must use clearly fake values and example domains.

If a credential is exposed, revoke or rotate it through its provider before attempting repository cleanup. Deleting a file from the current branch does not remove it from Git history, PR refs, releases, or downloaded copies.

## Safe diagnostics

Share the xswap and Codex versions, OS/Python version, the command with generic account labels, and a redacted error. `xswap auto-status` distinguishes saved settings from runtime support, but its account labels/PIDs still require review. Do not disable sandboxing or expand trusted paths just to make a report reproducible.

## Credential file checks

Before registration, automatic credential refresh, and OpenClaw sync, xswap requires `auth.json` to be a regular file owned by the current OS user with no group/other permission bits. Mode `0600` or read-only `0400` is accepted. Final-component symlinks, directories, and FIFOs are rejected. Validation and reading use the same open file descriptor. xswap does not silently chmod or replace source credentials.

If rejected, inspect the selected Codex home's `auth.json` locally. For a regular file you own, remove unintended sharing/ACLs and set `chmod 600 /path/to/codex-home/auth.json`, then retry. For a link, sign in to a dedicated home with a regular credential file. Do not post its contents. POSIX mode checks do not audit ACLs, ancestor-directory access, root, or other processes running as the same OS user; protect the home and backups too.
