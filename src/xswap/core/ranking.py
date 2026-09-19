"""Pure quota-window ranking: which account has the most headroom.

No state, no I/O and no `Manager` -- the rows come from the caller, so this
stays importable from `manager`, `tick` and the tests alike. Which bucket and
which window lengths "headroom" is measured in comes from the caller's
`QuotaShape` (`Manager.provider.quota_shape`); with none it is inferred from
the rows, which is what a pure caller with nothing but fixtures gets.
"""
from __future__ import annotations

from xswap.core.quota import buckets_available, quota_windows, short_percent, weekly_percent, window_percent  # noqa: F401
from xswap.core.usage import is_ok, window_label


def rank_candidates(rows, model=None, exclude=(), weekly_remaining=0, shape=None):
    """Pick the row with the most headroom: (name or None, {"remaining", "candidates"}).

    Shared by Manager.best_account (exhaustion only, weekly_remaining=0) and
    `xswap auto-tick` (the configured weekly reserve). Disabled and excluded rows are
    dropped; only rows with a successful fetch whose buckets_available(...) is True
    (every applicable window known and above the reserve) are ranked, by the tightest
    window's remaining percent and then by its earliest reset. `candidates` lists every
    considered row with its short/weekly remaining and short status; never tokens.

    `rows` are the dicts `Manager.account_rows()` produces; an `UsageSnapshot` is
    accepted in their place and read through the same keys (see `snapshot_row`).
    """
    rows = [snapshot_row(row) for row in rows]
    excluded = set(exclude)
    candidates = [row for row in rows if not row["disabled"] and row["name"] not in excluded]
    if not candidates:
        return None, {"remaining": {}, "candidates": []}
    summary, ranked = [], []
    for row in candidates:
        summary.append({"name": row["name"], "remaining5h": short_percent(row["buckets"], shape),
                        "remaining7d": weekly_percent(row["buckets"], shape), "status": row["status"]})
        if is_ok(row["status"]) and buckets_available(row["buckets"], model, weekly_remaining, shape) is True:
            ranked.append(row)
    if not ranked:
        return None, {"remaining": {}, "candidates": summary}

    def rank_key(row):
        known = [w for w in quota_windows(row["buckets"], shape) if w["remainingPercent"] is not None]
        if not known:
            return (0, float("inf"))  # defensive: buckets_available(...) is True already guarantees this
        tightest = min(known, key=lambda w: w["remainingPercent"])
        return (-tightest["remainingPercent"], tightest["resetsAt"] if tightest["resetsAt"] is not None else float("inf"))

    winner = min(ranked, key=rank_key)
    remaining = {window_label(w): w["remainingPercent"] for w in quota_windows(winner["buckets"], shape)}
    return winner["name"], {"remaining": remaining, "candidates": summary}


def snapshot_row(row):
    """A row dict for either input shape.

    `Manager.account_rows()` has always produced dicts, and everything that stores
    or prints them (`usage-cache.json`, `list --json`, the dashboard) still does.
    A provider that hands back an `UsageSnapshot` instead is adapted here rather
    than at every call site, so the two can coexist while the rest of xswap moves
    over (INT-5614).
    """
    if isinstance(row, dict):
        return row
    return {"name": row.account, "buckets": row.buckets, "status": "ok", "disabled": False,
            "fetchedAt": row.fetched_at, "resetCredits": row.reset_credits}
