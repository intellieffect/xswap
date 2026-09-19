"""The command line itself: parser assembly and dispatch, and nothing else.

`build_parser()` registers every subparser in the order argparse prints them,
and `main()` parses, builds the one `Manager`, looks the command up in
`COMMANDS` and translates the result into an exit code. The branch bodies live
in `commands/`, one module per group; none of them knows about argparse
assembly and none of them knows about the others.

`xswap.manager.parser` and `xswap.manager.main` forward here, so the console
scripts, the `codex_swap` shim and the test suite keep working unchanged.

Patchability: `Manager` is resolved through `xswap.manager` at call time below,
because the suite patches `xswap.manager.Manager` 31 times to hand `main()` a
manager on a temporary root. Everything else `main()` touches is either a
module attribute (`subprocess.TimeoutExpired`) or an exception class.

The command modules under `cli.commands` import helpers such as `identity`,
`upgrade`, `plain_codex_notice` and `validate_warn_threshold` from
`xswap.manager` at import time. Those used to be read from `manager`'s globals
inside `main()`, so `patch("xswap.manager.identity")` would have reached them;
it no longer does. No test patches them today. To make one patchable, resolve
it through `xswap.manager` at call time the way `Manager` is.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from xswap import manager as manager_module
from xswap._version import __version__
from xswap.alert import AlertError
from xswap.errors import SwapError
from xswap.live import LiveError
from xswap.upgrade import UpgradeError
from xswap.cli.commands import accounts, auto, launch, maintenance, mapping, openclaw, usage


def build_parser():
    p = argparse.ArgumentParser(description="Codex account switcher for CLI + macOS desktop. Bare xswap opens the selected CLI account.")
    p.add_argument("--version", action="version", version=f"xswap {__version__}")
    sub = p.add_subparsers(dest="command")
    # Registration order is the order `xswap --help` lists the subcommands in, and
    # tests/test_help_snapshots.py pins that output byte for byte. Keep it.
    maintenance.add_init_parser(sub)
    accounts.add_register_parser(sub)
    accounts.add_add_parser(sub)
    accounts.add_login_parser(sub)
    usage.add_list_parser(sub)
    usage.add_usage_parser(sub)
    usage.add_menubar_parser(sub)
    usage.add_dashboard_parser(sub)
    usage.add_status_parser(sub)
    auto.add_auto_status_parser(sub)
    auto.add_auto_tick_parser(sub)
    auto.add_auto_enable_parser(sub)
    auto.add_auto_policy_parser(sub)
    maintenance.add_repair_plugins_parser(sub)
    maintenance.add_relocate_codex_parser(sub)
    maintenance.add_doctor_parser(sub)
    auto.add_auto_disable_parser(sub)
    maintenance.add_upgrade_parser(sub)
    maintenance.add_alert_parser(sub)
    accounts.add_use_parser(sub)
    accounts.add_disable_parser(sub)
    accounts.add_enable_parser(sub)
    accounts.add_remove_parser(sub)
    mapping.add_map_parser(sub)
    mapping.add_unmap_parser(sub)
    openclaw.add_openclaw_parser(sub)
    launch.add_run_parser(sub)
    launch.add_app_parser(sub)
    maintenance.add_completion_parser(sub)
    return p


# Every named subcommand; `run` and the bare `xswap` fall through to
# launch.run_codex, which is the branch main()'s if/elif chain ended on.
COMMANDS = {
    "init": maintenance.run_init,
    "register": accounts.run_register,
    "add": accounts.run_add,
    "login": accounts.run_login,
    "list": usage.run_list,
    "usage": usage.run_usage,
    "menubar": usage.run_menubar,
    "dashboard": usage.run_dashboard,
    "status": usage.run_status,
    "auto-status": auto.run_auto_status,
    "auto-tick": auto.run_auto_tick,
    "auto-enable": auto.run_auto_enable,
    "auto-policy": auto.run_auto_policy,
    "repair-plugins": maintenance.run_repair_plugins,
    "relocate-codex": maintenance.run_relocate_codex,
    "doctor": maintenance.run_doctor,
    "auto-disable": auto.run_auto_disable,
    "upgrade": maintenance.run_upgrade,
    "alert": maintenance.run_alert,
    "use": accounts.run_use,
    "switch": accounts.run_use,
    "disable": accounts.run_disable,
    "enable": accounts.run_enable,
    "remove": accounts.run_remove,
    "map": mapping.run_map,
    "unmap": mapping.run_unmap,
    "openclaw": openclaw.run_openclaw,
    "app": launch.run_app,
    "completion": maintenance.run_completion,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        manager = manager_module.Manager()
        result = COMMANDS.get(args.command, launch.run_codex)(args, manager)
        return 0 if result is None else result
    except (SwapError, LiveError, UpgradeError, AlertError, OSError) as exc:
        print(f"xswap: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except subprocess.TimeoutExpired:
        print("xswap: Operation timed out; OpenClaw may be partially updated. Check xswap's auth backups and retry the same command. No existing sessions were stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
