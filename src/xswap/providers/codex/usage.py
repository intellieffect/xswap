"""Read Codex quota windows through the official app-server JSON-RPC API."""
from __future__ import annotations

from datetime import datetime
import json
import os
import selectors
import signal
import subprocess
import time


from xswap.core.errors import UsageError
from xswap.core.types import QuotaShape
from xswap.core.usage import (AUTH_FAILED_STATUS, SIGN_IN_REQUIRED, clean, is_ok, is_sign_in_failure,  # noqa: F401
                              number, reset_label, usage_lines, window_label)
from xswap.core import usage as _core_usage

# Where Codex's subscription quota sits in a normalized payload: the "codex" bucket,
# a five-hour rolling window and a seven-day one. This is the one place those numbers
# are written down; core takes them from here through `CodexProvider.quota_shape`.
CODEX_QUOTA = QuotaShape(bucket_id="codex", short_minutes=300, weekly_minutes=10080)




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
                        raise UsageError(SIGN_IN_REQUIRED)
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


def normalize_reset_credits(response):
    """Keep reset availability separate from quota buckets and purchased credits."""
    source = response.get("rateLimitResetCredits")
    if not isinstance(source, dict):
        return None
    count = source.get("availableCount")
    count = count if type(count) is int and count >= 0 else None
    details = source.get("credits")
    credits = None
    if isinstance(details, list):
        credits = []
        for row in details:
            if not isinstance(row, dict):
                continue
            expiry = row.get("expiresAt")
            credits.append({"status": clean(row["status"]) if row.get("status") else None,
                            "expiresAt": expiry if number(expiry) and expiry > 0 else None})
    return {"availableCount": count, "credits": credits}


def reset_credit_lines(credits):
    count = credits.get("availableCount") if credits else None
    lines = [f"codex reset credits: {count} available" if count is not None
             else "codex reset credits: unknown"]
    for row in (credits.get("credits") or []) if credits else []:
        if row["status"] != "available" or row["expiresAt"] is None:
            continue
        try:
            expiry = datetime.fromtimestamp(row["expiresAt"]).astimezone().strftime("%m/%d %H:%M %Z")
        except (ValueError, OverflowError, OSError):
            continue
        lines.append(f"  reset credit expires {expiry}")
    return lines


def warnings(rows, threshold, now=None):
    """`xswap list --warn` over Codex's quota bucket."""
    return _core_usage.warnings(rows, threshold, now, shape=CODEX_QUOTA)


def short_line(rows):
    """The prompt/status line over Codex's 5h and 7d windows."""
    return _core_usage.short_line(rows, shape=CODEX_QUOTA)
