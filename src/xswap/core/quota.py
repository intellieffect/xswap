"""Reading a normalized quota payload without knowing whose quota it is.

Every function here takes the buckets a provider produced plus the `QuotaShape`
that says which bucket and which window lengths they are. Pass no shape and the
helpers infer one (`QuotaShape()`): the first non-Spark bucket, windows
classified by duration. The inference exists for pure callers that only have
rows; `Manager.provider.quota_shape` is what production paths pass.

Nothing here does I/O, and nothing here names a platform.
"""
from __future__ import annotations

import math

from xswap.core.errors import LiveError
from xswap.core.types import QuotaShape, UsageBucket, UsageWindowPayload

# Extra buckets some plans carry alongside the subscription quota. They are
# reported next to it, are never the bucket a switch is decided on, and are
# opt-in in the list view (`--spark`).
EXTRA_BUCKET_HINT = "spark"


def is_extra(bucket: UsageBucket) -> bool:
    """True for a side bucket (Spark and friends), never for the quota bucket."""
    return EXTRA_BUCKET_HINT in (str(bucket.get("id") or "") + str(bucket.get("name") or "")).lower()


def quota_buckets(buckets: list[UsageBucket], shape: QuotaShape | None = None) -> list[UsageBucket]:
    """The bucket(s) a switch is decided on: the shape's, or the first non-extra one."""
    shape = shape or QuotaShape()
    if shape.bucket_id is not None:
        return [b for b in buckets if b.get("id") == shape.bucket_id]
    primary = next((b for b in buckets if not is_extra(b)), None)
    return [primary] if primary is not None else []


def quota_windows(buckets: list[UsageBucket], shape: QuotaShape | None = None) -> list[UsageWindowPayload]:
    """Every window of the quota bucket, flattened."""
    return [window for bucket in quota_buckets(buckets, shape) for window in bucket["windows"]]


def window_percent(buckets: list[UsageBucket], minutes: int, shape: QuotaShape | None = None) -> float | None:
    """The quota bucket's `remainingPercent` for the window of exactly MINUTES."""
    return next((w["remainingPercent"] for w in quota_windows(buckets, shape) if w["windowMinutes"] == minutes), None)


def weekly_window(buckets: list[UsageBucket], shape: QuotaShape | None = None) -> UsageWindowPayload | None:
    """The quota bucket's long window, or None when it was not reported."""
    shape = shape or QuotaShape()
    return next((w for w in quota_windows(buckets, shape) if shape.is_weekly(w)), None)


def weekly_percent(buckets: list[UsageBucket], shape: QuotaShape | None = None) -> float | None:
    window = weekly_window(buckets, shape)
    return window["remainingPercent"] if window else None


def short_percent(buckets: list[UsageBucket], shape: QuotaShape | None = None) -> float | None:
    """The quota bucket's short window, i.e. the one that is not the weekly one."""
    shape = shape or QuotaShape()
    window = next((w for w in quota_windows(buckets, shape) if not shape.is_weekly(w)), None)
    return window["remainingPercent"] if window else None


def reached(buckets: list[UsageBucket], shape: QuotaShape | None = None) -> str | None:
    """The quota bucket's own "limit reached" marker, or None."""
    return next((b["reached"] for b in quota_buckets(buckets, shape) if b["reached"]), None)


def validate_threshold(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value < 100:
        raise LiveError('weekly remaining threshold must be a number from 0 to less than 100')
    return float(value)


def buckets_available(
    buckets: list[UsageBucket],
    model: str | None = None,
    weekly_remaining: float = 0,
    shape: QuotaShape | None = None,
) -> bool | None:
    """True/False/None (unknown) for "this account can still serve a turn".

    Unknown is never available. Every applicable known window must be above
    zero, and -- when a reserve is configured -- the weekly window must be
    strictly above it. A model whose name carries the extra-bucket hint also
    needs that extra bucket to be present and healthy.
    """
    shape = shape or QuotaShape()
    applicable = quota_buckets(buckets, shape)
    if model and EXTRA_BUCKET_HINT in model.lower():
        applicable = list(applicable) + [b for b in buckets if is_extra(b)]
        if len(applicable) < 2:
            return None
    if not applicable:
        return None
    unknown = False
    for bucket in applicable:
        if bucket['reached']:
            return False
        if not bucket['windows']:
            unknown = True
        weekly_seen = False
        for window in bucket['windows']:
            left = window['remainingPercent']
            if left is None:
                unknown = True
            elif left <= 0:
                return False
            if shape.is_weekly(window):
                weekly_seen = True
                if left is not None and left <= weekly_remaining:
                    return False
        if weekly_remaining > 0 and not weekly_seen:
            unknown = True
    return None if unknown else True
