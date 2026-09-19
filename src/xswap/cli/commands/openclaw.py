"""`openclaw`: push an account or pool to the local OpenClaw agents."""
from __future__ import annotations

from pathlib import Path

from xswap.manager import SwapError  # import-time copies: not patch targets today (see cli/__init__ docstring)


def add_openclaw_parser(sub):
    o = sub.add_parser("openclaw", help="Sync an account (or pool) to local OpenClaw agents and reload Gateway auth")
    o.add_argument("name", nargs="?")
    o.add_argument("--pool", help="Comma-separated accounts (>=2) OpenClaw rotates between on its own cooldowns; mutually exclusive with NAME")
    o.add_argument("--allow-mixed", action="store_true", help="Allow --pool accounts from different ChatGPT organizations")
    o.add_argument("--agent", action="append", dest="agents", help="Target only this agent (repeatable; default: all)")
    o.add_argument("--dry-run", action="store_true")
    o.add_argument("--backup-dir", type=Path, help="Directory for small, private auth-state backups")
    o.add_argument("--clear-cooldown", action="store_true",
                    help="Clear a stale OpenClaw auth-profile cooldown for NAME/--pool (or every registered account), restarting the local Gateway")
    o.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    return o


def run_openclaw(args, manager):
    if args.pool and args.name:
        raise SwapError("--pool and a positional NAME are mutually exclusive.")
    if args.allow_mixed and not args.pool:
        raise SwapError("--allow-mixed requires --pool.")
    # Only split here; sync_openclaw's own parse_pool() call is the single place
    # that dedupes and enforces >=2 distinct names, so this list isn't re-validated.
    names = args.pool.split(",") if args.pool else args.name
    if args.clear_cooldown:
        if args.allow_mixed:
            raise SwapError("--allow-mixed has no effect with --clear-cooldown.")
        if args.agents:
            raise SwapError("--agent has no effect with --clear-cooldown.")
        manager.clear_openclaw_cooldown(names, dry=args.dry_run, yes=args.yes, backup_dir=args.backup_dir)
    else:
        if args.yes:
            raise SwapError("--yes requires --clear-cooldown.")
        manager.sync_openclaw(names, args.agents, args.dry_run, args.backup_dir, allow_mixed=args.allow_mixed)
