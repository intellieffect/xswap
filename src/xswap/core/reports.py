"""One-line summaries of a live-switch or login broadcast. Pure text, no state."""
from __future__ import annotations


def failure_reasons_text(report):
    """` Failed: reason; reason.` from the bridges' own classified reasons, deduplicated, or empty."""
    reasons = list(dict.fromkeys(r for r in report.get("reasons") or [] if isinstance(r, str) and r))
    return " Failed: " + "; ".join(reasons) + "." if reasons else ""


def describe_switch_report(report, name):
    """One line for `use`/`switch`: which live bridges took the selection, without a zero-filled dump."""
    unsafe = report.get("unsafe")
    if unsafe:
        return (f"Running sessions were not signalled: {unsafe} is not private (expected mode 700). "
                f"Fix: chmod 700 {unsafe} — the next xswap launch repairs it as well.")
    counts = {key: value for key, value in report.items() if value and key != "reasons"}
    if not counts:
        return f"No running bridged sessions; new sessions start as {name}."
    text = "Running bridged sessions: " + ", ".join(f"{key} {value}" for key, value in counts.items()) + "."
    if counts.get("pending"):
        text += " Pending requests apply when the current turn finishes."
    if counts.get("unsupported"):
        text += " Bridges older than 0.7.2 need reopening once."
    text += failure_reasons_text(report)
    if counts.get("failed") or counts.get("unconfirmed"):
        text += " Check xswap auto-status for the sessions that did not confirm."
    return text


def describe_login_report(report, name):
    """One line for `login`: which live bridges on NAME re-authenticated."""
    unsafe = report.get("unsafe")
    if unsafe:
        return (f"Running sessions were not signalled: {unsafe} is not private (expected mode 700). "
                f"Fix: chmod 700 {unsafe} — the next xswap launch repairs it as well.")
    counts = {key: value for key, value in report.items() if value and key != "reasons"}
    if not counts:
        return f"No running bridged session is on {name}; sessions on other accounts re-read it when needed."
    text = f"Running bridged sessions on {name}: " + ", ".join(f"{key} {value}" for key, value in counts.items()) + "."
    if counts.get("pending"):
        text += " Pending requests re-authenticate when the current turn finishes."
    if counts.get("unsupported"):
        text += " Bridges older than 0.7.2 need reopening once."
    text += failure_reasons_text(report)
    if counts.get("failed") or counts.get("unconfirmed"):
        text += " Check xswap auto-status for the sessions that did not confirm."
    return text
