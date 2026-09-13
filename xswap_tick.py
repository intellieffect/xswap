"""One proactive switching check for launchd/cron (`xswap auto-tick`).

Between sessions nothing else moves the default selection: a bridge only decides at
turn start, and `codex exec`/new sessions start on whatever `xswap use` last chose. This
command reads the pool and weekly reserve from auto.json, fetches quota the same way
`xswap list` does, and, when the selected account is at or below the reserve, selects
the best pool account exactly as `xswap use NAME` would (registry + pool order + a
manual-switch broadcast to running bridges).

Exit codes follow cswap's `auto --once`: 0 switched (or would switch with --dry-run),
1 error, 2 no action, 3 blocked. Stdout carries one classified line per decision
(`switched:` / `would-switch:` / `no-action:` / `blocked:`) so a wrapper can grep it;
names, percentages and short status reasons only, never tokens.
"""
from __future__ import annotations

import json

from codex_swap import describe_switch_report, is_auth_failed, rank_candidates, window_percent
from xswap_cli import read_settings
from xswap_live import buckets_available, validate_threshold
from xswap_usage import AUTH_FAILED_STATUS, SIGN_IN_REQUIRED, is_ok

EXIT_SWITCHED, EXIT_ERROR, EXIT_NO_ACTION, EXIT_BLOCKED = 0, 1, 2, 3

# Row statuses that mean this login cannot serve a new session at all, as opposed to a
# transient usage-service failure. identity(), read_limits() and the auth-state record
# produce exactly these. AUTH_FAILED_STATUS matters most: `alert --auto-switch` always
# runs with --cached, and a cached row for a rejected login carries only that status.
SIGN_IN_STATUSES = ("not signed in", "unreadable auth cache", AUTH_FAILED_STATUS,
                    "usage unavailable: " + SIGN_IN_REQUIRED)


def percent(value):
    return f"{value:g}%" if isinstance(value, (int, float)) and not isinstance(value, bool) else "unknown"


def weekly_left(row):
    return window_percent(row["buckets"], 10080) if is_ok(row["status"]) else None


def describe_shortfall(row, reserve):
    """Why the selected account no longer qualifies; mirrors buckets_available's False cases."""
    if row["status"] in SIGN_IN_STATUSES:
        return f"{row['name']} sign-in required"
    left = weekly_left(row)
    if left is not None and left <= reserve:
        return f"{row['name']} weekly {percent(left)} left, at or below the {reserve:g}% reserve"
    reached = next((b["reached"] for b in row["buckets"] if b["id"] == "codex" and b["reached"]), None)
    if reached:
        return f"{row['name']} limit reached ({reached})"
    return f"{row['name']} has an exhausted codex window"


def decide(rows, current, pool, reserve, failed=frozenset()):
    """Pure decision over already-fetched Manager.account_rows() output.

    Returns {"decision": "switch"|"no-action"|"blocked", "reason": <short code>,
    "target": name|None, "message": <one line without the switched:/would-switch: prefix>,
    "remaining": {name: weekly-left|None}, "candidates": [...]}.

    Unknown quota never causes a switch (the same rule as the bridge's before_turn and
    `run --best`), except when the selected login is known to be unusable: a
    sign-in-required status, or auth-failed per the auth-state record (`failed`).
    Candidates are the pool minus the current, disabled, and auth-failed accounts; only
    one that is strictly above the reserve (buckets_available(...) is True) can be chosen.
    """
    by_name = {row["name"]: row for row in rows}
    remaining = {name: weekly_left(by_name[name]) for name in dict.fromkeys([current, *pool]) if name in by_name}
    result = {"decision": "no-action", "reason": "", "target": None, "message": "", "remaining": remaining, "candidates": []}
    row = by_name.get(current)
    if row is None:
        return dict(result, decision="blocked", reason="no-selection",
                    message="blocked: no account is selected; run: xswap use NAME")
    if current in failed or row["status"] in SIGN_IN_STATUSES:
        state = False
    elif is_ok(row["status"]):
        state = buckets_available(row["buckets"], None, reserve)
    else:
        state = None
    if state is True:
        return dict(result, reason="above-reserve",
                    message=f"no-action: {current} weekly {percent(weekly_left(row))} left is above the {reserve:g}% reserve")
    if state is None:
        why = row["status"] if not is_ok(row["status"]) else "weekly window not reported"
        return dict(result, reason="quota-unknown",
                    message=f"no-action: {current} quota unknown ({why}); not switching on unknown quota")
    candidates = [by_name[name] for name in pool if name in by_name and name != current and name not in failed]
    target, detail = rank_candidates(candidates, weekly_remaining=reserve)
    result["candidates"] = detail["candidates"]
    shortfall = describe_shortfall(row, reserve)
    if target is None:
        listed = ", ".join(f"{c['name']} {percent(c['remaining7d'])}" for c in detail["candidates"]) or "none"
        return dict(result, decision="blocked", reason="no-candidate",
                    message=f"blocked: {shortfall}, but no pool account is above the {reserve:g}% reserve ({listed})")
    return dict(result, decision="switch", reason="switched", target=target,
                message=f"{current} -> {target} ({shortfall}; {target} weekly {percent(remaining.get(target))} left)")


def run_tick(manager, dry_run=False, max_age=None, json_output=False):
    """`xswap auto-tick`: fetch, decide, and apply through the `xswap use NAME` path."""
    settings = read_settings(manager)
    reserve = validate_threshold(settings.get("weeklyRemainingThreshold", 0))
    current = manager.read()["active"]
    pool = [name for name in settings.get("accounts", []) if isinstance(name, str)]
    if current is None:
        outcome = {"decision": "blocked", "reason": "no-selection", "target": None, "remaining": {}, "candidates": [],
                   "message": "blocked: no account is selected; run: xswap use NAME"}
    elif not settings.get("enabled"):
        names = ",".join(name for name, _ in manager.enabled_accounts()) or "NAME,NAME"
        outcome = {"decision": "blocked", "reason": "auto-disabled", "target": None, "remaining": {}, "candidates": [],
                   "message": f"blocked: automatic switching is not enabled; run: xswap auto-enable --accounts {names}"}
    else:
        rows = manager.account_rows(max_age=max_age)
        failed = frozenset(name for name in dict.fromkeys([current, *pool]) if is_auth_failed(manager, name))
        outcome = decide(rows, current, pool, reserve, failed)
    lines, report = [], None
    if outcome["decision"] == "switch":
        target = outcome["target"]
        if dry_run:
            outcome["reason"] = "would-switch"
            lines.append("would-switch: " + outcome["message"])
        elif manager.read()["active"] != current:
            # A manual `xswap use` landed while quota was being read; it wins.
            outcome.update(decision="no-action", reason="selection-changed", target=None,
                           message=f"no-action: the selection changed from {current} while quota was being read; nothing changed")
            lines.append(outcome["message"])
        else:
            from xswap_switch import switch_running
            manager.use(target)
            report = switch_running(manager, target)
            lines.append("switched: " + outcome["message"])
            lines.append(describe_switch_report(report, target))
    else:
        lines.append(outcome["message"])
    code = {"switch": EXIT_SWITCHED, "no-action": EXIT_NO_ACTION, "blocked": EXIT_BLOCKED}[outcome["decision"]]
    if json_output:
        decision = outcome["reason"] if outcome["decision"] == "switch" else outcome["decision"]
        payload = {"decision": decision, "reason": outcome["reason"], "exitCode": code, "selected": current,
                   "target": outcome["target"], "reserve": reserve, "dryRun": dry_run,
                   "remaining": outcome["remaining"], "candidates": outcome["candidates"], "report": report, "lines": lines}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return code
