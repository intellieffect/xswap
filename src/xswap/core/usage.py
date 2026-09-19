"""Quota status vocabulary and the formatting every platform's rows share.

The transport that fetches quota, and the normalization of whatever that
transport answers, belong to a provider. What is left -- the two status
strings the rest of xswap compares against, and the pure formatting of an
already-normalized payload -- is the same whichever platform produced it, so
it lives here and is re-exported by the provider module that used to own it.

Anything that has to know *which* bucket or *which* window carries the
subscription quota takes a `QuotaShape` (see `xswap.core.quota`).
"""
from __future__ import annotations

from datetime import datetime
import math
import time

from xswap.core import quota
from xswap.core.errors import UsageError  # noqa: F401  re-exported: this module's error class
from xswap.core.types import DAY_MINUTES, QuotaShape

# A usage fetch classified as "this login was rejected" (401-class), in the
# wording it has had since 0.7.6. A fetch classified this way is recorded per
# account in auth-state.json: before 0.8.0 a rejected login was visible only in
# the one live `xswap list` that hit it.
SIGN_IN_REQUIRED = "sign in again to read usage"
# Row status served from that record by cached/offline views; never a live fetch result.
AUTH_FAILED_STATUS = "sign-in required"


def is_sign_in_failure(error):
    """True only for the sign-in classification above, never for outages or timeouts."""
    return isinstance(error, UsageError) and str(error) == SIGN_IN_REQUIRED


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean(value):
    return "".join(c for c in str(value) if c.isprintable())[:100]


def window_label(window):
    minutes = window["windowMinutes"]
    if minutes is None:
        return window["position"]
    if minutes % 1440 == 0:
        return f"{minutes / 1440:g}d"
    if minutes % 60 == 0:
        return f"{minutes / 60:g}h"
    return f"{minutes:g}m"


def reset_label(timestamp, now):
    if timestamp is None:
        return "reset unknown"
    try:
        clock = datetime.fromtimestamp(timestamp).astimezone().strftime("%m/%d %H:%M %Z")
    except (ValueError, OverflowError, OSError):
        return "reset unknown"
    minutes = max(0, math.ceil((timestamp - now) / 60))
    if minutes == 0:
        return f"reset pending ({clock})"
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    duration = f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"
    return f"resets {clock} (in {duration})"


def is_ok(status):
    """True for a successful quota fetch, whether live or served from the local cache."""
    return status in ("ok", "ok (cached)")


def warnings(rows, threshold, now=None, shape=None):
    """Pure evaluation for `list --warn`: which quota windows are below threshold.

    Only non-disabled rows (``row.get("disabled")`` falsy) with a successful status
    (``is_ok``: live or cached) are considered. A window only warns when its
    ``remainingPercent`` is a known number below ``threshold``; unknown values never
    trigger a warning.
    """
    now = time.time() if now is None else now
    lines = []
    for row in rows:
        if row.get("disabled") or not is_ok(row.get("status")):
            continue
        for window in quota.quota_windows(row.get("buckets", []), shape):
            remaining = window.get("remainingPercent")
            if not number(remaining) or remaining >= threshold:
                continue
            lines.append(f"warn: {row.get('name')} {window_label(window)} {remaining:g}% left "
                         f"({reset_label(window.get('resetsAt'), now)})")
    return lines


def short_line(rows, shape=None):
    """One line per account for a status line/prompt: `{*}{name} {short}/{weekly}`, joined by ' · '.

    Pure formatting over rows shaped like show_accounts' output (name, active, status,
    buckets); no subprocess, no I/O. The two numbers are the quota bucket's short and
    weekly window `remainingPercent`, rounded half up to an integer. Unknown or
    unavailable is "?".
    """
    shape = shape or QuotaShape()
    parts = []
    for row in rows:
        marker = "*" if row.get("active") else ""
        primary = secondary = "?"
        if is_ok(row.get("status")):
            # Classify by duration, not position: a server may report only a
            # seven-day window and still call it "primary". A window that gives no
            # duration at all is the one case where position decides.
            short = weekly = None
            for window in quota.quota_windows(row.get("buckets", []), shape):
                minutes = window.get("windowMinutes")
                if window.get("position") == "secondary" if minutes is None else minutes >= DAY_MINUTES:
                    weekly = weekly or window
                else:
                    short = short or window
            if short and short.get("remainingPercent") is not None:
                primary = str(math.floor(short["remainingPercent"] + 0.5))
            if weekly and weekly.get("remainingPercent") is not None:
                secondary = str(math.floor(weekly["remainingPercent"] + 0.5))
        parts.append(f"{marker}{row['name']} {primary}/{secondary}")
    return " · ".join(parts)


def usage_lines(buckets, now=None):
    now = time.time() if now is None else now
    lines = []
    for bucket in buckets:
        label = bucket["name"]
        for window in bucket["windows"]:
            remaining = window["remainingPercent"]
            value = f"{remaining:g}% left" if remaining is not None else "remaining unknown"
            lines.append(f"{label} {window_label(window)}: {value} · {reset_label(window['resetsAt'], now)}")
        if bucket["reached"]:
            lines.append(f"{label}: limit reached ({bucket['reached']})")
        credits = bucket["credits"]
        if credits and credits["unlimited"]:
            lines.append(f"{label} credits: unlimited")
        elif credits and credits["balance"] is not None:
            lines.append(f"{label} credits: {credits['balance']}")
    return lines or ["quota windows not reported"]
