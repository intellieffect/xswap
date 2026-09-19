"""Automatic switching: `auto-status`, `auto-tick`, `auto-enable`, `auto-policy`, `auto-disable`."""
from __future__ import annotations

from xswap.manager import parse_cache_seconds  # import-time copies: not patch targets today (see cli/__init__ docstring)


def add_auto_status_parser(sub):
    st = sub.add_parser("auto-status", help="Show desktop/CLI automatic switching state (no credentials)")
    st.add_argument("--prune", action="store_true", help="Remove every non-running CLI run record now, not only stopped ones older than a day, empty ones older than a minute, and others older than 7 days")
    return st


def add_auto_tick_parser(sub):
    tick = sub.add_parser("auto-tick", help="One proactive check for launchd/cron: if the selected account is at or below the weekly reserve, select the best pool account (exit 0 switched, 1 error, 2 no action, 3 blocked)")
    tick.add_argument("--dry-run", action="store_true", help="Print the decision without selecting or signalling anything")
    tick.add_argument("--cached", metavar="SECONDS", help="Reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    tick.add_argument("--json", action="store_true", dest="json_output", help="Machine-readable decision (names, percentages, short reasons; no tokens)")
    return tick


def add_auto_enable_parser(sub):
    ae = sub.add_parser("auto-enable", help="Enable automatic switching for new xswap CLI/app sessions")
    ae.add_argument("--accounts", required=True)
    ae.add_argument("--wrap-codex", action="store_true", help="Also wrap the user-owned codex symlink, with rollback metadata")
    return ae


def add_auto_policy_parser(sub):
    policy = sub.add_parser("auto-policy", help="Set weekly remaining percentage for proactive switching")
    policy.add_argument("--weekly-remaining", type=float, required=True)
    return policy


def add_auto_disable_parser(sub):
    return sub.add_parser("auto-disable", help="Disable auto defaults and restore the codex symlink")


def run_auto_status(args, manager):
    from xswap.codex_cli import show_status
    show_status(manager, args.prune)


def run_auto_tick(args, manager):
    from xswap.tick import run_tick
    return run_tick(manager, dry_run=args.dry_run, max_age=parse_cache_seconds(args.cached), json_output=args.json_output)


def run_auto_enable(args, manager):
    from xswap.codex_cli import enable
    enable(manager, args.accounts, args.wrap_codex)


def run_auto_policy(args, manager):
    from xswap.codex_cli import set_policy
    set_policy(manager, args.weekly_remaining)


def run_auto_disable(args, manager):
    from xswap.codex_cli import disable
    disable(manager)
