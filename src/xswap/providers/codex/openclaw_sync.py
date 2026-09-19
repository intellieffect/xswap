"""OpenClaw credential sync and cooldown clearing.

Both entry points take `Manager.locked()` themselves, once, around the whole
subprocess sequence -- the OpenClaw SDK bridge plus the Gateway reload, or the
Gateway stop/write/start -- exactly as they did on `Manager`.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from xswap.core.errors import SwapError
from xswap.core.fsutil import atomic_json
from xswap.core.path import absolute_which


def parse_pool(value):
    """Split "a, b,a" (or dedupe an already-split list) into >=2 distinct, ordered names."""
    parts = value.split(",") if isinstance(value, str) else value
    seen = []
    for part in parts:
        part = part.strip() if isinstance(part, str) else part
        if part and part not in seen:
            seen.append(part)
    if len(seen) < 2:
        raise SwapError("--pool needs at least two distinct registered account names.")
    return seen


def resolve_openclaw_package_root(executable):
    """Walk up from the openclaw executable to its package.json (name: "openclaw")."""
    for directory in Path(executable).resolve().parents:
        package = directory / "package.json"
        if package.is_file():
            try:
                if json.loads(package.read_text()).get("name") == "openclaw":
                    return directory
            except (OSError, ValueError):
                pass
    return None


class OpenClawSync:
    def __init__(self, manager, hooks):
        self.manager = manager
        self.hooks = hooks

    def sync(self, names=None, agents=None, dry=False, backup_dir=None, select=False, allow_mixed=False):
        # Directory mappings scope a single launched session; OpenClaw sync mutates
        # shared agent state, so an omitted/pooled name must resolve through the
        # active account only, never a directory mapping.
        pool = self.hooks.parse_pool(names) if isinstance(names, list) else [names]
        resolved = []
        for entry in pool:
            entry_name, entry_home = self.manager.account(entry, mapped=False)
            self.manager.require_enabled(entry_name)
            self.hooks.check_file_store(entry_home)
            resolved.append((entry_name, entry_home))
        if len(resolved) > 1 and not allow_mixed:
            groups = {}
            for entry_name, entry_home in resolved:
                org = self.hooks.chatgpt_org_id(entry_home, entry_name)
                groups.setdefault(org, []).append(entry_name)
            if len(groups) > 1:
                pretty = ", ".join("[" + ", ".join(names_in_group) + "]" for names_in_group in groups.values())
                raise SwapError(f"Pooled accounts belong to different ChatGPT organizations: {pretty}. OpenClaw shares one agent's conversation context across whatever it selects from the pool, so mixing organizations mixes their context across accounts. Pass --allow-mixed to override.")
        name, home = resolved[0]
        # absolute_which refuses a relative answer instead of returning it: from a repository
        # whose PATH starts with `bin`, shutil.which handed back `bin/node`, and the two
        # subprocesses below ran that project's own file with the account's CODEX_HOME -- and
        # with every pooled account's codexHome on its stdin -- while `openclaw secrets reload`
        # went to the same directory's `bin/openclaw`.
        executable = absolute_which("openclaw", error=SwapError)
        node = absolute_which("node", error=SwapError)
        if not executable or not node:
            raise SwapError("OpenClaw sync requires openclaw and node in PATH.")
        package_root = self.hooks.resolve_openclaw_package_root(executable)
        if package_root is None:
            raise SwapError("Cannot locate OpenClaw's installed package through its executable. Use the standard npm installation.")
        helper = Path(__file__).resolve().parent / "bridge" / "openclaw.mjs"
        # Keep the single-account "account"/"codexHome" keys byte-compatible; "accounts" carries the full pool.
        request = {"account": name, "codexHome": str(home),
                   "accounts": [{"name": entry_name, "codexHome": str(entry_home)} for entry_name, entry_home in resolved],
                   "packageRoot": str(package_root), "agents": agents or [], "dryRun": dry,
                   "backupRoot": str(Path(backup_dir).expanduser().resolve() if backup_dir else self.manager.root / "backups" / "openclaw")}
        # Serialize xswap mutations across the bridge + Gateway reload. OpenClaw also locks its own stores.
        with self.manager.locked():
            result = subprocess.run([node, str(helper)], input=json.dumps(request), text=True,
                                    capture_output=True, env=self.manager.env(home), timeout=90)
            try:
                output = json.loads(result.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise SwapError("OpenClaw SDK bridge failed. Check the installed OpenClaw version (tested: 2026.8.1).") from None
            if result.returncode or output.get("error"):
                raise SwapError(output.get("error", "OpenClaw credential update failed."))
            if dry:
                print(json.dumps(output, indent=2))
                return output
            reload_result = subprocess.run([executable, "secrets", "reload", "--json"],
                                           capture_output=True, text=True, env=self.manager.env(home), timeout=45)
            try:
                reload_json = json.loads(reload_result.stdout)
            except ValueError:
                reload_json = {}
            if reload_result.returncode or reload_json.get("ok") is not True:
                raise SwapError(f"OpenClaw auth was saved for {len(output['completed'])} agents, but Gateway reload failed. Start/check the local Gateway, then run: openclaw secrets reload. Backup: {output['backup']}")
            if reload_json.get("warningCount", 0):
                raise SwapError("OpenClaw auth was saved and reloaded with warnings. Run openclaw secrets reload --json and inspect them before continuing.")
            if select:
                data = self.manager.read()
                data["active"] = name
                atomic_json(self.manager.registry, data)
        label = ", ".join(entry_name for entry_name, _ in resolved)
        rotates = " to rotate on its own cooldowns" if len(resolved) > 1 else ""
        print(f"OpenClaw now selects {label} for {len(output['completed'])} agents{rotates}; Gateway auth reloaded.\nBackup: {output['backup']}")
        return output

    def clear_cooldown(self, names=None, dry=False, yes=False, backup_dir=None):
        """Clear a stale OpenClaw auth-profile cooldown (one 429 lasts until its reset
        time even after the real limit is gone) for the given account(s), or every
        registered account when none are named. Stops the local Gateway before writing
        and restarts it after; a failed stop aborts before the database is touched.
        """
        from xswap.providers.codex.openclaw_state import OpenClawStateError, clear_cooldown, default_sqlite_path, format_until, profile_id_for_home, read_cooldowns

        data = self.manager.read()
        if names:
            pool = names if isinstance(names, list) else [names]
            entries = []
            for entry in pool:
                entry_name, entry_home = self.manager.account(entry, mapped=False)
                entries.append((entry_name, entry_home))
        else:
            entries = [(name, Path(value["home"])) for name, value in data["accounts"].items()]
        if not entries:
            raise SwapError("No registered accounts to check.")

        sqlite_path = default_sqlite_path()
        if not sqlite_path.exists():
            raise SwapError(f"OpenClaw state database not found: {sqlite_path}")
        try:
            cooldowns = read_cooldowns(sqlite_path)
        except OpenClawStateError as exc:
            raise SwapError(str(exc)) from None

        targets = []  # [(name, profile_id)]
        for entry_name, entry_home in entries:
            profile_id = profile_id_for_home(entry_home)
            if profile_id and profile_id in cooldowns:
                targets.append((entry_name, profile_id))

        if not targets:
            print("No stale OpenClaw cooldowns found.")
            return {"cleared": []}

        for entry_name, profile_id in targets:
            info = cooldowns[profile_id]
            reason = info.get("blockedReason") or "unknown"
            print(f"{entry_name} ({profile_id}): blocked {format_until(info['blockedUntil'])} ({reason})")

        if dry:
            print("Dry run: no changes made; the Gateway was not stopped.")
            return {"cleared": [], "dryRun": True}

        # The same lookup, and the same stop/start of the user's Gateway through whatever the
        # working directory holds if it is allowed to be relative.
        executable = absolute_which("openclaw", error=SwapError)
        if not executable:
            raise SwapError("OpenClaw sync requires openclaw in PATH.")

        if not yes:
            if not sys.stdin.isatty():
                raise SwapError("Confirm with --yes.")
            prompt = f"Stop the Gateway and clear {len(targets)} OpenClaw cooldown(s)? [y/N] "
            if input(prompt).strip().lower() != "y":
                print("Aborted.")
                return {"cleared": [], "aborted": True}
        backup_root = Path(backup_dir).expanduser().resolve() if backup_dir else self.manager.root / "backups" / "openclaw-cooldown"

        with self.manager.locked():
            stop = subprocess.run([executable, "gateway", "stop", "--force"],
                                  capture_output=True, text=True, env=self.manager.env(self.manager.source), timeout=45)
            if stop.returncode:
                raise SwapError("OpenClaw Gateway stop failed; the database was not touched. "
                                 f"{(stop.stderr or stop.stdout).strip() or 'unknown error'}")
            try:
                backup_path = clear_cooldown(sqlite_path, [pid for _, pid in targets], backup_root)
            except OpenClawStateError as exc:
                raise SwapError(f"Cooldown clear failed after the Gateway was stopped; restart it manually: "
                                 f"openclaw gateway start. {exc}") from None
            start = subprocess.run([executable, "gateway", "start"],
                                   capture_output=True, text=True, env=self.manager.env(self.manager.source), timeout=45)
            if start.returncode:
                raise SwapError(f"Cleared the cooldown (backup: {backup_path}), but OpenClaw Gateway start failed; "
                                 f"run: openclaw gateway start. {(start.stderr or start.stdout).strip() or 'unknown error'}")

        cleared = [entry_name for entry_name, _ in targets]
        print(f"Cleared {len(targets)} OpenClaw cooldown(s): {', '.join(cleared)}.\nBackup: {backup_path}")
        return {"cleared": cleared, "backup": str(backup_path)}
