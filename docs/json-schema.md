# `--json` payload schema (INT-5611)

Every machine-readable payload xswap emits is built by exactly one function in
[`src/xswap/json_output.py`](../src/xswap/json_output.py), so this document and that
module are the single source of truth for the shape.

`xswap.json_output.SCHEMA_VERSION` is the contract version for the **object-shaped**
payloads below. It is bumped only for a breaking change: a field renamed, removed, or
changed in type/meaning. Adding a new field is not breaking and does not bump it.

Two shapes exist:

- **Object payloads** (a top-level JSON object) carry a leading `"schemaVersion"`
  integer key, inserted before every other field.
- **List payloads** (a top-level JSON array) stay bare arrays — wrapping them in an
  object would break every existing consumer that does `json.loads(...)` and indexes or
  iterates the result directly. These are documented as `v1-list`: their contract
  version is implied by the xswap release and this document, not carried in the
  payload itself.

## `list --json` / `usage --json` — v1-list

Builder: `accounts_payload(rows)`. Shape: a bare JSON array of account rows (unchanged
since before this contract existed). One entry per account (`list`) or the one named/
selected account (`usage NAME`).

| Field | Type | Nullable | Meaning |
|---|---|---|---|
| `name` | string | no | Account key as registered (e.g. `main`, `work`). |
| `identity` | string | no | Login label: an email, `"not signed in"`, `"unreadable auth cache"`, or `"API key"`. |
| `status` | string | no | One of the status sentinels below. |
| `buckets` | array of objects | no (empty when unknown) | Normalized quota windows (`id`, `usedPercent`, `windowMinutes`, `reached`). |
| `resetCredits` | number | yes | Reset-credit balance if the account's plan reports one. |
| `fetchedAt` | number (unix seconds) | yes | When `buckets` was last fetched; `null` if never fetched. |
| `disabled` | boolean | no | Whether the account is excluded from the automatic pool. |
| `cached` | boolean | no | Whether this row came from `usage-cache.json` rather than a live fetch. |
| `slot` | integer | no | 1-based registration order, stable across renames. |
| `active` | boolean | no | Whether this is the currently selected account. |

### Status sentinels (the `status` field's values)

Re-exported as named constants from `xswap.json_output` (and, for backward
compatibility, still importable from the modules that originally defined them:
`xswap.usage`, `xswap.usage_cache`).

| Constant | Value | Meaning |
|---|---|---|
| `STATUS_OFFLINE` | `"offline"` | Row built with `--offline`; no fetch attempted. |
| `STATUS_DISABLED` | `"disabled"` | Account is disabled; no fetch attempted. |
| `STATUS_OK` | `"ok"` | Live fetch succeeded. |
| `STATUS_OK_CACHED` | `"ok (cached)"` | Served from `usage-cache.json` within `--cached SECONDS`. |
| `STATUS_API_KEY` | `"API key: subscription quota not available"` | Login uses an API key; no subscription quota to report. |
| `STATUS_NOT_SIGNED_IN` | `"not signed in"` | No auth cache for this account. |
| `STATUS_UNREADABLE_AUTH_CACHE` | `"unreadable auth cache"` | Auth cache present but unreadable/corrupt. |
| `STATUS_AUTH_FAILED` (`AUTH_FAILED_STATUS`) | `"sign-in required"` | Cached/offline view of a login the usage service has already rejected. |
| — | `"usage unavailable: <error>"` | Live fetch failed; `<error>` is the raised message, e.g. `"usage unavailable: sign in again to read usage"` (see `STATUS_SIGN_IN_REQUIRED`) or `"usage unavailable: cannot start Codex CLI"`. |

## `doctor --json` — v1-list

Builder: `doctor_payload(results)`. Shape: a bare JSON array of check results, one per
diagnostic.

| Field | Type | Nullable | Meaning |
|---|---|---|---|
| `name` | string | no | Check identifier, e.g. `"codex binary"`, `"registry"`, `"storage"`. |
| `status` | string | no | One of `"OK"`, `"WARN"`, `"FAIL"`. |
| `detail` | string | no | Human-readable detail or remediation hint. |

## `auto-status` — object, schemaVersion 1

JSON-only command (no `--json` flag; it always prints JSON). Builder:
`auto_status_payload(state)`.

| Field | Type | Nullable | Meaning |
|---|---|---|---|
| `schemaVersion` | integer | no | Contract version (see above). |
| `enabled` | boolean | no | Whether automatic switching is enabled. |
| `accounts` | array of strings | no | Configured fallback pool, in order. |
| `codexWrapped` | boolean | no | Whether the `codex` entry point is currently wrapped. |
| `wrapperReason` | string | no | Why `codexWrapped` has that value (e.g. `"connected"`, a bypass or drift reason). |
| `weeklyRemainingThreshold` | number | no | Configured reserve percentage for `auto-tick`. |
| `pruned` | integer | no | Count of run records removed during this call. |
| `prunedRuns` | array | no | The pruned run records themselves. |
| `sessions` | array of objects | no | Live/known bridge sessions (`account`, `surface`, `running`, `bridgeVersion`, …). |

## `auto-tick --json` — object, schemaVersion 1

Builder: `auto_tick_payload(payload)`.

| Field | Type | Nullable | Meaning |
|---|---|---|---|
| `schemaVersion` | integer | no | Contract version (see above). |
| `decision` | string | no | `"switch"`/`"switched"`/`"would-switch"`, `"no-action"`, or `"blocked"`. |
| `reason` | string | no | Short machine-readable code, e.g. `"above-reserve"`, `"quota-unknown"`, `"no-candidate"`, `"switched"`. |
| `exitCode` | integer | no | The process exit code for this decision (see [docs/exit-codes.md](exit-codes.md)). |
| `selected` | string | yes | The account selected before this tick ran. |
| `target` | string | yes | The account switched to, or `null` if none. |
| `reserve` | number | no | The weekly-remaining reserve threshold in effect. |
| `dryRun` | boolean | no | Whether `--dry-run` was passed (no selection change was made). |
| `remaining` | object (name → number\|null) | no | Weekly-remaining percent per considered account. |
| `candidates` | array of objects | no | Pool accounts considered as a switch target, each with `name`/`remaining7d`. |
| `report` | object | yes | Bridge-switch report when a switch actually happened; `null` otherwise. |
| `lines` | array of strings | no | The same human-readable lines printed in non-JSON mode. |

## `dashboard` — object, schemaVersion 1

JSON-only command (no `--json` flag), read by the macOS menu bar app
(`src/xswap/bridge/MenuBar.swift`) every five minutes. Builder: `dashboard_payload(data)`.

| Field | Type | Nullable | Meaning |
|---|---|---|---|
| `schemaVersion` | integer | no | Contract version (see above). |
| `accounts` | array of objects | no | Per-account display summaries (already localized for `--lang`). |
| `policy` | string | no | Automatic-switching policy label. |
| `sessions` | array of objects | no | Currently running bridge sessions (`account`, `surface`). |
| `updatedAt` | number (unix seconds) | no | When this payload was built. |

`MenuBar.swift` decodes this payload with `JSONSerialization`/keyed lookups rather than a
strict `Decodable` struct that rejects unknown keys, so the added `schemaVersion` field
does not require a Swift change (see the INT-5611 PR description for the exact call
sites checked).
