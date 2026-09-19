"""Pure quota-window ranking: which account has the most codex headroom.

No state, no I/O and no `Manager` -- the rows come from the caller, so this
stays importable from `manager`, `tick` and the tests alike.
"""
from __future__ import annotations

from xswap.live import buckets_available
from xswap.usage import is_ok, window_label


def codex_windows(buckets):
    return [window for bucket in buckets if bucket["id"] == "codex" for window in bucket["windows"]]


def window_percent(buckets, minutes):
    return next((w["remainingPercent"] for w in codex_windows(buckets) if w["windowMinutes"] == minutes), None)


def rank_candidates(rows, model=None, exclude=(), weekly_remaining=0):
    """Pick the row with the most codex headroom: (name or None, {"remaining", "candidates"}).

    Shared by Manager.best_account (exhaustion only, weekly_remaining=0) and
    `xswap auto-tick` (the configured weekly reserve). Disabled and excluded rows are
    dropped; only rows with a successful fetch whose buckets_available(...) is True
    (every applicable window known and above the reserve) are ranked, by the tightest
    window's remaining percent and then by its earliest reset. `candidates` lists every
    considered row with its 5h/7d remaining and short status; never tokens.
    """
    excluded = set(exclude)
    candidates = [row for row in rows if not row["disabled"] and row["name"] not in excluded]
    if not candidates:
        return None, {"remaining": {}, "candidates": []}
    summary, ranked = [], []
    for row in candidates:
        summary.append({"name": row["name"], "remaining5h": window_percent(row["buckets"], 300),
                        "remaining7d": window_percent(row["buckets"], 10080), "status": row["status"]})
        if is_ok(row["status"]) and buckets_available(row["buckets"], model, weekly_remaining) is True:
            ranked.append(row)
    if not ranked:
        return None, {"remaining": {}, "candidates": summary}

    def rank_key(row):
        known = [w for w in codex_windows(row["buckets"]) if w["remainingPercent"] is not None]
        if not known:
            return (0, float("inf"))  # defensive: buckets_available(...) is True already guarantees this
        tightest = min(known, key=lambda w: w["remainingPercent"])
        return (-tightest["remainingPercent"], tightest["resetsAt"] if tightest["resetsAt"] is not None else float("inf"))

    winner = min(ranked, key=rank_key)
    remaining = {window_label(w): w["remainingPercent"] for w in codex_windows(winner["buckets"])}
    return winner["name"], {"remaining": remaining, "candidates": summary}
