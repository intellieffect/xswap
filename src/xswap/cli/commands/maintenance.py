"""Setup and upkeep: `init`, `doctor`, `upgrade`, `alert`, `repair-plugins`,
`relocate-codex` and `completion`."""
from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from xswap._version import __version__
from xswap.alert import install as alert_install
from xswap.alert import status as alert_status
from xswap.alert import uninstall as alert_uninstall
from xswap.manager import (  # import-time copies: not patch targets today (see cli/__init__ docstring)
    SwapError,
    upgrade,
)

if TYPE_CHECKING:
    from xswap.manager import Manager


def add_init_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    ini = sub.add_parser("init", help="First-run setup: register, add accounts, enable auto mode, set policy, then doctor")
    ini.add_argument("--yes", action="store_true", help="Non-interactive: accept every safe default, prompt for nothing")
    ini.add_argument("--no-auto", action="store_true", dest="no_auto", help="Skip the automatic-switching step")
    ini.add_argument("--weekly-remaining", type=float, help="Weekly remaining percentage for auto-policy (default 10)")
    return ini


def add_repair_plugins_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    rp = sub.add_parser("repair-plugins", help="Materialize legacy shared plugin links without stopping sessions")
    rp.add_argument("--dry-run", action="store_true")
    return rp


def add_relocate_codex_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    rc = sub.add_parser("relocate-codex", help="Move a Codex release that an in-session update installed inside xswap's state directory to the reference Codex home, and link xswap's homes to it")
    rc.add_argument("--dry-run", action="store_true", help="Print the planned moves without changing anything")
    return rc


def add_doctor_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    dr = sub.add_parser("doctor", help="Diagnose codex install, accounts, plugins, and OpenClaw (read-only, no network)")
    dr.add_argument("--json", action="store_true", dest="json_output")
    return dr


def add_upgrade_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    up = sub.add_parser("upgrade", help="Reinstall xswap from the latest (or a chosen) released Git tag")
    up.add_argument("--tag", help="Install this tag instead of the latest release, e.g. v0.5.1")
    up.add_argument("--dry-run", action="store_true")
    return up


def add_alert_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    al = sub.add_parser("alert", help="Install, remove, or inspect a launchd job that polls `list --warn` and posts macOS notifications")
    al.add_argument("--install", action="store_true", help="Write the wrapper script and plist, then load it")
    al.add_argument("--uninstall", action="store_true", help="Unload the job and delete the wrapper script and plist")
    al.add_argument("--status", action="store_true", help="Show whether the job is installed and loaded, and the last log tail")
    al.add_argument("--dry-run", action="store_true", help="Print what --install/--uninstall would do without changing anything")
    al.add_argument("--warn", type=float, default=15, metavar="PCT", help="Threshold passed to `list --warn` (1-100, default 15)")
    al.add_argument("--every", type=int, default=30, metavar="MINUTES", help="Polling interval in whole minutes (default 30; launchd merges under 60s)")
    al.add_argument("--cached", type=float, default=600, metavar="SECONDS", help="Freshness passed to `list --cached` (default 600)")
    al.add_argument("--auto-switch", action="store_true", help="With --install: run `xswap auto-tick --cached SECONDS` before the warn step and post a notification when it switches")
    return al


def add_completion_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    comp = sub.add_parser("completion", help="Print a shell completion script (see xswap_completion.py)")
    comp.add_argument("shell", choices=["zsh", "bash"])
    return comp


def run_init(args: argparse.Namespace, manager: Manager) -> int:
    from xswap.init import run_init as init
    return init(manager, args)


def run_repair_plugins(args: argparse.Namespace, manager: Manager) -> None:
    from xswap.plugins import repair
    repair(manager, args.dry_run)


def run_relocate_codex(args: argparse.Namespace, manager: Manager) -> None:
    from xswap.relocate import relocate
    relocate(manager, dry=args.dry_run)


def run_doctor(args: argparse.Namespace, manager: Manager) -> int:
    from xswap.doctor import print_report, run
    return print_report(run(manager), args.json_output)


def run_upgrade(args: argparse.Namespace, manager: Manager) -> int:
    return upgrade(__version__, args.tag, args.dry_run)


def run_alert(args: argparse.Namespace, manager: Manager) -> int:
    if sum((args.install, args.uninstall, args.status)) != 1:
        raise SwapError("Give exactly one of --install, --uninstall, or --status.")
    if args.auto_switch and not args.install:
        raise SwapError("--auto-switch requires --install.")
    if args.install:
        return alert_install(manager.root, warn=args.warn, every=args.every, cached=args.cached,
                             auto_switch=args.auto_switch, dry_run=args.dry_run)
    if args.uninstall:
        return alert_uninstall(manager.root, dry_run=args.dry_run)
    return alert_status(manager.root)


def run_completion(args: argparse.Namespace, manager: Manager) -> None:
    from xswap.cli import build_parser
    from xswap.completion import generate
    sys.stdout.write(generate(build_parser(), args.shell))
