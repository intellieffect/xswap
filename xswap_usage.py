"""Read Codex quota windows through the official app-server JSON-RPC API."""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
import selectors
import signal
import subprocess
import time


class UsageError(Exception):
    pass


def read_limits(codex, env, timeout=12):
    """Start only our own server; never create a thread/turn or change accounts."""
    process = subprocess.Popen([codex, "app-server", "--stdio"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=env, bufsize=0, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    buffer = b""

    def send(message):
        payload = (json.dumps(message) + "\n").encode()
        process.stdin.write(payload)

    def receive(request_id):
        nonlocal buffer
        while True:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    raise UsageError("invalid Codex response") from None
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message["error"] or {}
                    # Classify only; raw server errors may contain sensitive request details.
                    if isinstance(error, dict) and error.get("code") == -32601:
                        raise UsageError("update Codex CLI to read usage")
                    description = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
                    if any(word in description for word in ("401", "unauthorized", "sign in", "authentication", "not authenticated")):
                        raise UsageError("sign in again to read usage")
                    raise UsageError("usage service unavailable")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise UsageError("invalid Codex response")
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise UsageError("usage request timed out")
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise UsageError("Codex usage server exited")
            buffer += chunk
            if len(buffer) > 2_000_000:
                raise UsageError("Codex response exceeded size limit")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "xswap_usage", "version": "0.2.0"},
            "capabilities": {"experimentalApi": True}}})
        receive(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        return receive(2)
    except (BrokenPipeError, OSError):
        raise UsageError("cannot communicate with Codex usage server") from None
    finally:
        selector.close()
        # This process group belongs solely to the server created above.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        process.stdin.close()
        process.stdout.close()


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean(value):
    return "".join(c for c in str(value) if c.isprintable())[:100]


def normalize_limits(response):
    """Whitelist display fields; never serialize a raw authenticated response."""
    by_id = response.get("rateLimitsByLimitId")
    source = list(by_id.items()) if isinstance(by_id, dict) and by_id else []
    source.sort(key=lambda item: (item[0] != "codex", str(item[0])))
    fallback = response.get("rateLimits")
    if not source and isinstance(fallback, dict):
        source = [(fallback.get("limitId") or "codex", fallback)]
    buckets = []
    for key, bucket in source:
        if not isinstance(bucket, dict):
            continue
        windows = []
        for position in ("primary", "secondary"):
            window = bucket.get(position)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if not number(used) or used < 0:
                used = None
            minutes = window.get("windowDurationMins")
            reset = window.get("resetsAt")
            windows.append({"position": position,
                            "usedPercent": used if number(used) else None,
                            "remainingPercent": max(0, min(100, 100 - used)) if number(used) else None,
                            "windowMinutes": minutes if number(minutes) and minutes > 0 else None,
                            "resetsAt": reset if number(reset) and reset > 0 else None})
        credits = bucket.get("credits")
        buckets.append({"id": clean(bucket.get("limitId") or key),
                        "name": clean(bucket.get("limitName") or key),
                        "plan": clean(bucket["planType"]) if bucket.get("planType") else None,
                        "windows": windows,
                        "reached": clean(bucket["rateLimitReachedType"]) if bucket.get("rateLimitReachedType") else None,
                        "credits": {"unlimited": credits.get("unlimited") is True,
                                    "balance": clean(credits["balance"]) if credits.get("balance") is not None else None}
                        if isinstance(credits, dict) else None})
    return buckets


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


def warnings(rows, threshold, now=None):
    """Pure evaluation for `list --warn`: which codex windows are below threshold.

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
        for bucket in row.get("buckets", []):
            if bucket.get("id") != "codex":
                continue
            for window in bucket.get("windows", []):
                remaining = window.get("remainingPercent")
                if not number(remaining) or remaining >= threshold:
                    continue
                lines.append(f"warn: {row.get('name')} {window_label(window)} {remaining:g}% left "
                             f"({reset_label(window.get('resetsAt'), now)})")
    return lines


def short_line(rows):
    """One line per account for a status line/prompt: `{*}{name} {p5h}/{p7d}`, joined by ' · '.

    Pure formatting over rows shaped like show_accounts' output (name, active, status,
    buckets); no subprocess, no I/O. p5h/p7d are the codex bucket's short (under a day)
    and weekly (a day or longer, by windowMinutes) window remainingPercent, rounded half
    up to an integer; position is only a fallback when windowMinutes is absent. Unknown
    or unavailable is "?".
    """
    parts = []
    for row in rows:
        marker = "*" if row.get("active") else ""
        primary = secondary = "?"
        if is_ok(row.get("status")):
            bucket = next((b for b in row.get("buckets", []) if (b.get("id") or "").lower() == "codex"), None)
            if bucket:
                # Classify by duration, not position: the server may report only a
                # seven-day window and still call it "primary".
                short = weekly = None
                for window in bucket.get("windows", []):
                    minutes = window.get("windowMinutes")
                    if minutes is None:
                        is_weekly = window.get("position") == "secondary"
                    else:
                        is_weekly = minutes >= 1440
                    if is_weekly:
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
