"""Reading quotas and state: `list`, `usage`, `status`, `dashboard`, `menubar`."""
from __future__ import annotations

import json
import os
import sys

from xswap.display import resolve_lang
from xswap.manager import SwapError, identity, parse_cache_seconds, usage_warnings, validate_warn_threshold


def add_list_parser(sub):
    listing = sub.add_parser("list", help="List accounts with live remaining quotas and reset times")
    listing.add_argument("--offline", action="store_true", help="Show local account labels without fetching usage")
    listing.add_argument("--json", action="store_true", dest="json_output")
    listing.add_argument("--include-spark", action="store_true", help="Include Spark quotas in text output")
    listing.add_argument("--details", action="store_true", help="Show identity and all quota window details")
    listing.add_argument("--short", action="store_true", help="Print one line for a status line/prompt: `*name p5h/p7d · ...`")
    listing.add_argument("--warn", metavar="PCT",
                          help="Print a warning per codex window below PCT remaining (1-100) to stderr and exit 3")
    listing.add_argument("--lang", choices=["en", "ko"], help="Text output language")
    # Registered last, as it was when it sat after the `usage` block in one flat parser().
    listing.add_argument("--cached", metavar="SECONDS", help="Reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex (not with --offline)")
    return listing


def add_usage_parser(sub):
    usage = sub.add_parser("usage", help="Show live quota windows for the selected or named account")
    usage.add_argument("name", nargs="?")
    usage.add_argument("--json", action="store_true", dest="json_output")
    usage.add_argument("--include-spark", action="store_true", help="Include Spark quotas in text output")
    usage.add_argument("--details", action="store_true", help="Show identity and all quota window details")
    usage.add_argument("--short", action="store_true", help="Print one line: `name p5h/p7d`")
    usage.add_argument("--lang", choices=["en", "ko"], help="Text output language")
    usage.add_argument("--cached", metavar="SECONDS", help="Reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    return usage


def add_menubar_parser(sub):
    return sub.add_parser("menubar", help="Build and open the macOS weekly quota menu")


def add_dashboard_parser(sub):
    dash = sub.add_parser("dashboard", help="Private presentation JSON for the menu app")
    dash.add_argument("--lang", choices=["en", "ko"], help="Text output language")
    return dash


def add_status_parser(sub):
    return sub.add_parser("status", help="Show selected account and login status")


def run_list(args, manager):
    threshold = validate_warn_threshold(args.warn) if args.warn is not None else None
    max_age = parse_cache_seconds(args.cached)
    if args.offline and max_age is not None:
        raise SwapError("--offline and --cached cannot be combined.")
    lang = resolve_lang(args.lang or "en", os.environ)
    rows = manager.show_accounts(offline=args.offline, json_output=args.json_output, include_spark=args.include_spark, details=args.details, short=args.short, max_age=max_age, lang=lang)
    if threshold is not None:
        messages = usage_warnings(rows, threshold)
        for message in messages:
            print(message, file=sys.stderr)
        return 3 if messages else 0


def run_usage(args, manager):
    max_age = parse_cache_seconds(args.cached)
    name, _ = manager.account(args.name)
    lang = resolve_lang(args.lang, os.environ)
    manager.show_accounts(name=name, json_output=args.json_output, include_spark=args.include_spark, details=args.details, short=args.short, max_age=max_age, lang=lang)


def run_dashboard(args, manager):
    from xswap.display import dashboard
    lang = resolve_lang(args.lang, os.environ)
    print(json.dumps(dashboard(manager, lang=lang), ensure_ascii=False))


def run_menubar(args, manager):
    from xswap.menubar import launch
    return launch()


def run_status(args, manager):
    default_name, mapped = manager.resolve_default()
    name, home = manager.account(default_name)
    lines = [f"Selected: {name}", f"Home: {home}", f"Local label: {identity(home)}"]
    if mapped:
        lines.append(f"Mapped by: {mapped}")
    print("\n".join(lines), flush=True)
    return manager.launch_cli(name, ["login", "status"])
