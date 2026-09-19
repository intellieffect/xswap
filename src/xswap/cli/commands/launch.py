"""Starting Codex: `run` (also the bare `xswap` form) and `app`."""
from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from xswap.manager import (  # import-time copies: not patch targets today (see cli/__init__ docstring)
    SwapError,
    parse_cache_seconds,
)

if TYPE_CHECKING:
    from xswap.manager import Manager


def add_run_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    r = sub.add_parser("run", help="Run Codex with the selected or named account")
    r.add_argument("--account"); r.add_argument("--dry-run", action="store_true")
    r.add_argument("--auto", action="store_true", help="Keep the interactive CLI session alive across quota account switches")
    r.add_argument("--accounts", help="Explicit automatic fallback order")
    r.add_argument("--best", action="store_true", help="Launch with the account with the most remaining quota right now (one-shot; not live switching)")
    r.add_argument("--model", help="Model hint for --best; never passed to codex")
    r.add_argument("--cached", metavar="SECONDS", help="With --best, reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    r.add_argument("args", nargs=argparse.REMAINDER)
    return r


def add_app_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    a = sub.add_parser("app", help="Open an account-specific desktop instance")
    a.add_argument("name", nargs="?"); a.add_argument("--app"); a.add_argument("--dry-run", action="store_true")
    a.add_argument("--auto", action="store_true", help="Keep one desktop session and switch accounts after quota exhaustion (experimental)")
    a.add_argument("--accounts", help="Explicit fallback order, e.g. work,main; requires --auto")
    return a


def run_app(args: argparse.Namespace, manager: Manager) -> int:
    if args.auto:
        if args.name:
            raise SwapError("Use --accounts instead of a positional name with --auto.")
        return manager.launch_auto_app(args.accounts, args.app, args.dry_run)
    if args.accounts:
        raise SwapError("--accounts requires --auto.")
    from xswap.codex_cli import read_settings
    settings = read_settings(manager)
    if args.name is None and settings.get("enabled"):
        return manager.launch_auto_app(','.join(settings["accounts"]), args.app, args.dry_run)
    return manager.launch_app(args.name, args.app, args.dry_run)


def run_codex(args: argparse.Namespace, manager: Manager) -> int:
    """`xswap run ...` and the bare `xswap`, which is why every field is read with getattr."""
    rest = getattr(args, "args", [])
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if getattr(args, "best", False):
        if args.account or getattr(args, "auto", False):
            raise SwapError("--best cannot be combined with --account or --auto.")
        if getattr(args, "accounts", None):
            raise SwapError("--accounts requires --auto.")
        max_age = parse_cache_seconds(getattr(args, "cached", None))
        name, reason = manager.best_account(getattr(args, "model", None), max_age=max_age)
        if name is None:
            name, _ = manager.account()
            print(f"xswap: no account with known remaining quota; using {name}", file=sys.stderr)
        if args.dry_run:
            _, home = manager.account(name)
            print(json.dumps({"account": name, "reason": reason, "CODEX_HOME": str(home),
                              "argv": [manager.codex(), *rest]}, indent=2))
            return 0
        return manager.launch_cli(name, rest, False)
    if getattr(args, "cached", None) is not None:
        raise SwapError("--cached requires --best.")
    if getattr(args, "auto", False):
        if args.account or not args.accounts:
            raise SwapError("Use --auto --accounts first,second without --account.")
        from xswap.codex_cli import launch_cli
        return launch_cli(manager, args.accounts, rest, args.dry_run)
    if getattr(args, "accounts", None):
        raise SwapError("--accounts requires --auto.")
    return manager.launch_cli(getattr(args, "account", None), rest, getattr(args, "dry_run", False))
