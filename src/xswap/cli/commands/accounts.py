"""Registering, signing in to, selecting and dropping accounts."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess  # noqa: F401  patched as xswap.manager.subprocess.call, the same module object
import sys

from xswap.display import resolve_lang
# Through `manager`, which is where these have always lived and where the suite
# reaches them; `SwapError` and `describe_switch_report` are its re-exports.
from xswap.manager import SwapError, describe_switch_report, identity, parse_cache_seconds, plain_codex_notice  # import-time copies: not patch targets today (see cli/__init__ docstring)


def add_register_parser(sub):
    r = sub.add_parser("register", help="Register an existing signed-in Codex home without copying its tokens")
    r.add_argument("name"); r.add_argument("--home", type=Path)
    return r


def add_add_parser(sub):
    a = sub.add_parser("add", help="Sign in to an isolated account home")
    a.add_argument("name"); a.add_argument("--device-auth", action="store_true")
    a.add_argument("--prepare-only", action="store_true")
    a.add_argument("--use", action="store_true", help="Select this account immediately after signing in")
    return a


def add_login_parser(sub):
    lg = sub.add_parser("login", help="Re-authenticate a registered account whose login expired")
    lg.add_argument("name"); lg.add_argument("--device-auth", action="store_true")
    return lg


def add_use_parser(sub):
    u = sub.add_parser("use", aliases=["switch"], help="Select the default account for xswap and xswap app")
    u.add_argument("name", nargs="?", help="Account name; switch also accepts a 1-based slot from xswap list")
    u.add_argument("--best", action="store_true", help="Select the account with the most remaining quota right now")
    u.add_argument("--model", help="Model hint for --best; never passed to codex")
    u.add_argument("--cached", metavar="SECONDS", help="With --best, reuse a cached quota lookup if fresher than SECONDS instead of spawning Codex")
    u.add_argument("--default-only", action="store_true", help="Change only the default for future sessions")
    u.add_argument("--openclaw", action="store_true", help="Also update all local OpenClaw agents and reload Gateway auth")
    return u


def add_disable_parser(sub):
    d = sub.add_parser("disable", help="Hold an account out of selection without deleting it")
    d.add_argument("name")
    return d


def add_enable_parser(sub):
    en = sub.add_parser("enable", help="Restore a disabled account to selection")
    en.add_argument("name")
    return en


def add_remove_parser(sub):
    rm = sub.add_parser("remove", help="Drop an account's registry entry; files are kept unless --purge")
    rm.add_argument("name")
    rm.add_argument("--purge", action="store_true", help="Also delete the managed profile directory (a registered home is never deleted)")
    rm.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    return rm


def run_register(args, manager):
    home = manager.register(args.name, args.home)
    print(f"Registered {args.name}: {identity(home)}. Credentials stay at {home}.")


def run_add(args, manager):
    home = manager.prepare(args.name)
    if args.prepare_only:
        print(f"Prepared {args.name}. Sign in: xswap add {args.name}")
        return 0
    if identity(home) not in ("not signed in", "unreadable auth cache"):
        raise SwapError(f"{args.name} already has a login. Re-authenticate with: xswap login {args.name}")
    command = [manager.codex(), "login"] + (["--device-auth"] if args.device_auth else [])
    result = subprocess.call(command, env=manager.env(home))
    if result:
        return result
    label = identity(home)
    if label in ("not signed in", "unreadable auth cache"):
        raise SwapError(f"codex login exited successfully, but {args.name} has no readable login at {home}. The sign-in may have been cancelled; run xswap add {args.name} again.")
    print(f"Saved {args.name}: {label}. Select it: xswap use {args.name}")
    manager.after_login(args.name, home, lang=resolve_lang(getattr(args, "lang", None), os.environ))
    # The account must be signed in before it can become active, so this only runs after login succeeds.
    if args.use:
        manager.use(args.name)
        print(f"Selected {args.name}.")
    elif manager.select_if_unset(args.name):
        print(f"Selected {args.name} (first account).")


def run_login(args, manager):
    return manager.login(args.name, args.device_auth, lang=resolve_lang(getattr(args, "lang", None), os.environ))


def run_use(args, manager):
    if bool(args.name) == bool(args.best):
        raise SwapError("Give an account name or --best.")
    if args.model and not args.best:
        raise SwapError("--model requires --best.")
    if args.cached is not None and not args.best:
        raise SwapError("--cached requires --best.")
    detail = ""
    if args.best:
        max_age = parse_cache_seconds(args.cached)
        name, reason = manager.best_account(args.model, max_age=max_age)
        if name is None:
            raise SwapError("No account with known remaining quota; nothing selected.")
        parts = [f"{label} {value:g}% left" for label, value in reason["remaining"].items() if value is not None]
        if parts:
            detail = f" ({', '.join(parts)})"
    else:
        name = manager.switch_target(args.name) if args.command == "switch" else args.name
    if args.openclaw:
        manager.sync_openclaw(name, select=True)
    else:
        manager.use(name)
    print(f"Selected {name}{detail}. CLI: xswap · Desktop: xswap app")
    notice = plain_codex_notice(manager, manager.account(name)[1])
    if notice:
        print(notice)
    if not args.default_only:
        from xswap.switch import switch_running
        report = switch_running(manager, name)
        print(describe_switch_report(report, name))
        if report.get('failed') or report.get('unconfirmed') or report.get('unsafe'):
            return 1


def run_disable(args, manager):
    manager.set_disabled(args.name, True)
    print(f"Disabled {args.name}. It is skipped by use, --auto pools, and live usage fetches. Restore it: xswap enable {args.name}")


def run_enable(args, manager):
    manager.set_disabled(args.name, False)
    print(f"Enabled {args.name}. Select it: xswap use {args.name}")


def run_remove(args, manager):
    name, _ = manager.account(args.name)
    entry = manager.read()["accounts"][name]
    will_purge = args.purge and entry.get("managed")
    # The same directory remove() will delete, refused here when the record names
    # another one -- before the prompt offers to delete it.
    target = manager.purge_target(name, entry) if will_purge else None
    if args.purge and not entry.get("managed"):
        print(f"--purge is ignored for {name}: it is a registered home, not a managed profile.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SwapError("Confirm with --yes.")
        prompt = (f"Remove {name} from xswap and delete {target}? [y/N] " if will_purge
                  else f"Remove {name} from xswap? [y/N] ")
        if input(prompt).strip().lower() != "y":
            print("Aborted.")
            return 1
    result = manager.remove(name, purge=args.purge)
    if result["purged"]:
        print(f"Removed {name}. Deleted the managed profile at {target}.")
    else:
        print(f"Removed {name} from xswap. Files kept at {result['kept']}.")
