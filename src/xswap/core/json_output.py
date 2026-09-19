"""Centralized builders for every user-facing `--json` payload and JSON-only command.

Each payload gets one builder here so the shape is defined in exactly one place and can
be versioned. `SCHEMA_VERSION` is the contract version for the payloads built here: bump
it only for a breaking change (a field renamed, removed, or changed type/meaning).
Adding a new field is not breaking and does not require a bump.

Object-shaped payloads (a top-level dict) carry a leading `"schemaVersion"` key, inserted
first so dict ordering puts it before the existing fields. List-shaped payloads
(`list`/`usage --json`, `doctor --json`) stay bare JSON lists -- wrapping them in an
object would break every existing consumer that expects `json.loads(...)` to hand back a
list -- and are documented as "v1-list": their contract version is implied by the CLI
version and this module, not carried in the payload itself.

This module only imports leaf/helper modules (never `cli`, `manager`, or a provider) so
it can be imported from anywhere without a cycle.
"""
from __future__ import annotations

from typing import Any

from xswap.core.types import AccountUsageRow, CheckPayload
from xswap.core.usage import AUTH_FAILED_STATUS, SIGN_IN_REQUIRED

SCHEMA_VERSION = 1

# --- Status sentinels that appear as DATA (the "status" field) in JSON payloads. ---
# Re-exported here as the single documented list of every literal a payload's "status"
# field can hold; the modules that assign them keep re-exporting the names they already
# imported so existing `from xswap.core.usage import ...` / `from xswap.core.usage_cache import ...`
# call sites and tests keep working unchanged.
STATUS_OFFLINE = "offline"
STATUS_DISABLED = "disabled"
STATUS_OK = "ok"
STATUS_OK_CACHED = "ok (cached)"
STATUS_API_KEY = "API key: subscription quota not available"
STATUS_NOT_SIGNED_IN = "not signed in"
STATUS_UNREADABLE_AUTH_CACHE = "unreadable auth cache"
STATUS_AUTH_FAILED = AUTH_FAILED_STATUS  # "sign-in required"
STATUS_SIGN_IN_REQUIRED = SIGN_IN_REQUIRED  # "sign in again to read usage" (raised, not a row status)


def _versioned(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with `schemaVersion` first, then every key of `payload` in order."""
    return {"schemaVersion": SCHEMA_VERSION, **payload}


def accounts_payload(rows: list[AccountUsageRow]) -> list[AccountUsageRow]:
    """`list --json` / `usage --json`: v1-list, unchanged bare list of account rows."""
    return rows


def doctor_payload(results: list[CheckPayload]) -> list[CheckPayload]:
    """`doctor --json`: v1-list, unchanged bare list of check results."""
    return results


def auto_status_payload(state: dict[str, Any]) -> dict[str, Any]:
    """`auto-status`: JSON-only command (no --json flag). Object; schemaVersion first."""
    return _versioned(state)


def auto_tick_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """`auto-tick --json`: object; schemaVersion first."""
    return _versioned(payload)


def dashboard_payload(data: dict[str, Any]) -> dict[str, Any]:
    """`dashboard`: JSON-only command, read by MenuBar.swift. Object; schemaVersion first."""
    return _versioned(data)
