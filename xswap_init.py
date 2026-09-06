"""`xswap init`: a thin first-run wizard over the existing standalone commands.

Every step below calls the same Manager method (or xswap_cli helper) its equivalent
standalone command already uses (register, prepare/login/select_if_unset for add,
xswap_cli.enable for auto-enable, xswap_cli.set_policy for auto-policy, xswap_doctor.run
for doctor). No new state is introduced and no registry logic is duplicated here.

All prompts go through `ask(prompt, default)` so tests can script exact answers without
a real TTY; the default `default_ask` reads one line from stdin.
"""
from __future__ import annotations

import subprocess
import sys

from codex_swap import SwapError, check_file_store, identity

REGISTER_PROMPT = "Register the existing Codex login as an account? name"
ADD_PROMPT = "Add another account? name (blank to skip)"
AUTO_PROMPT_TEMPLATE = "Enable automatic switching across {names} and connect the codex command? [Y/n]"
POLICY_PROMPT_TEMPLATE = "Set the weekly quota reserve to {pct:g}% remaining? [Y/n]"

NOT_SIGNED_IN = ("not signed in", "unreadable auth cache")


def default_ask(prompt, default=None):
    """Read one scripted answer from stdin; an empty line accepts `default`."""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{suffix}: ")
    except EOFError:
        raw = ""
    raw = raw.strip()
    return raw if raw else (default or "")


def _yes(answer, default_yes=True):
    answer = (answer or "").strip().lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def _account_names(manager):
    return list(manager.read()["accounts"].keys())


def _step_register(manager, args, ask, interactive, lines):
    home = manager.source
    try:
        check_file_store(home)
    except SwapError as exc:
        lines.append(f"register: skipped ({exc})")
        return
    if identity(home) in NOT_SIGNED_IN:
        lines.append(f"register: skipped (no readable login at {home}; run: codex login)")
        return
    if any(entry["home"] == str(home) for entry in manager.read()["accounts"].values()):
        lines.append("register: skipped (this Codex home is already registered)")
        return
    if args.yes or not interactive:
        name = "main"
    else:
        name = ask(REGISTER_PROMPT, "main")
    if not name:
        lines.append("register: skipped (no name given)")
        return
    try:
        manager.register(name, home)
    except SwapError as exc:
        lines.append(f"register: skipped ({exc})")
        return
    lines.append(f"register: registered {name} ({identity(home)})")


def _step_add(manager, args, ask, interactive, lines):
    if args.yes or not interactive:
        lines.append("add: skipped (--yes) -- sign in to another account later: xswap add work")
        return
    added = []
    while True:
        name = ask(ADD_PROMPT, "")
        if not name:
            break
        try:
            home = manager.prepare(name)
        except SwapError as exc:
            lines.append(f"add {name}: skipped ({exc})")
            continue
        if identity(home) not in NOT_SIGNED_IN:
            lines.append(f"add {name}: skipped (already has a login)")
            continue
        result = subprocess.call([manager.codex(), "login"], env=manager.env(home))
        if result:
            lines.append(f"add {name}: codex login failed (exit {result})")
            continue
        label = identity(home)
        if label in NOT_SIGNED_IN:
            lines.append(f"add {name}: codex login exited but no readable login was saved")
            continue
        # Selection stays with the first account: select_if_unset only claims an unset default.
        manager.select_if_unset(name)
        added.append(name)
        lines.append(f"add: added {name} ({label})")
    if not added:
        lines.append("add: no additional accounts -- sign in to another account later: xswap add work")


def _step_auto(manager, args, ask, interactive, lines):
    from xswap_cli import enable, read_settings
    from xswap_live import LiveError

    names = _account_names(manager)
    if args.no_auto:
        lines.append("auto: skipped (--no-auto)")
        return
    if len(names) < 2:
        lines.append(f"auto: skipped (need at least 2 accounts; have {len(names)})")
        return
    settings = read_settings(manager)
    if settings.get("enabled") and set(settings.get("accounts", [])) == set(names):
        lines.append("auto: already enabled across " + ", ".join(names))
        return
    prompt = AUTO_PROMPT_TEMPLATE.format(names=", ".join(names))
    if args.yes:
        confirmed = True
    elif interactive:
        confirmed = _yes(ask(prompt, "y"))
    else:
        confirmed = False
    if not confirmed:
        lines.append("auto: skipped (not confirmed)")
        return
    try:
        enable(manager, ",".join(names), wrap=True)
        lines.append("auto: enabled across " + ", ".join(names) + " (codex command connected)")
    except (SwapError, LiveError) as exc:
        lines.append(f"auto: skipped ({exc})")


def _step_policy(manager, args, ask, interactive, lines):
    from xswap_cli import read_settings, set_policy

    settings = read_settings(manager)
    if not settings.get("enabled"):
        lines.append("policy: skipped (automatic switching is not enabled)")
        return
    pct = args.weekly_remaining if args.weekly_remaining is not None else 10
    prompt = POLICY_PROMPT_TEMPLATE.format(pct=pct)
    if args.yes:
        confirmed = True
    elif interactive:
        confirmed = _yes(ask(prompt, "y"))
    else:
        confirmed = False
    if not confirmed:
        lines.append("policy: skipped (not confirmed)")
        return
    set_policy(manager, pct)
    lines.append(f"policy: weekly reserve set to {pct:g}%")


def run_init(manager, args, ask=None, interactive=None):
    """Run register -> add -> auto -> policy -> doctor in order. Returns doctor's exit code."""
    from xswap_doctor import print_report, run as run_doctor

    if ask is None:
        ask = default_ask
    if interactive is None:
        interactive = sys.stdin.isatty() and not args.yes

    lines = []
    _step_register(manager, args, ask, interactive, lines)
    _step_add(manager, args, ask, interactive, lines)
    _step_auto(manager, args, ask, interactive, lines)
    _step_policy(manager, args, ask, interactive, lines)
    print("\n".join(lines))
    print()
    return print_report(run_doctor(manager))
