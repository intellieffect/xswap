"""usage-cache.json and the quota rows built on top of it.

`forget` assumes the caller holds `Manager.locked()` (that is how `Registry.remove`
and `Launcher.after_login` keep their cross-concern sequence under one lock);
`remember` takes the lock itself, exactly as `Manager.remember_usage` did.

`read_limits`, `identity` and `check_file_store` are reached through `hooks`,
which resolves them in `xswap.manager`'s namespace at call time, so the suite's
`patch("xswap.manager.read_limits")` still reaches this code.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

from xswap._version import __version__
from xswap.errors import SwapError
from xswap.fsutil import atomic_json
from xswap.ranking import rank_candidates
from xswap.json_output import accounts_payload
from xswap.usage import AUTH_FAILED_STATUS, UsageError, is_sign_in_failure, normalize_limits, normalize_reset_credits, short_line


class UsageCache:
    def __init__(self, manager, hooks):
        self.manager = manager
        self.hooks = hooks

    def path(self):
        return self.manager.root / "usage-cache.json"

    def cached(self, name, label, max_age):
        """Return {"buckets", "fetchedAt"} from a still-fresh cache entry, or None.

        A saved entry whose identity label no longer matches the account's current
        login (a re-login under the same name) is treated as a miss, not reused.
        """
        if not max_age or max_age <= 0:
            return None
        try:
            data = json.loads(self.manager.usage_cache_path().read_text())
        except (OSError, ValueError):
            return None
        entry = data.get(name) if isinstance(data, dict) else None
        if not isinstance(entry, dict) or entry.get("identity") != label:
            return None
        fetched_at, buckets = entry.get("fetchedAt"), entry.get("buckets")
        if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool) or not isinstance(buckets, list):
            return None
        if time.time() - fetched_at > max_age:
            return None
        return {"buckets": buckets, "fetchedAt": fetched_at, "resetCredits": entry.get("resetCredits")}

    def forget(self, name):
        """Drop a removed account's cache entry (label may be an email). Caller holds the lock."""
        try:
            data = json.loads(self.manager.usage_cache_path().read_text())
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.pop(name, None) is not None:
            atomic_json(self.manager.usage_cache_path(), data)

    def still_registered(self, name):
        """Whether NAME is still in the registry. Caller holds the lock.

        A usage fetch takes seconds, and `remove` drops the account's cache and auth-state
        entries under the same lock; a fetch already in flight then wrote them back after the
        removal, so `usage-cache.json` and `auth-state.json` kept naming an account that no
        longer exists -- with its identity label, which is an email -- and a later `xswap add`
        under the same name inherited the removed login's rejection. An unreadable registry is
        someone else's error to report: keep the write rather than drop data over it.
        """
        try:
            return name in self.manager.read()["accounts"]
        except SwapError:
            return True

    def remember(self, name, buckets, fetched_at, label, reset_credits=None):
        """Whitelisted normalized fields only; never raw responses or tokens. Always 0600."""
        with self.manager.locked():
            if not self.manager._still_registered(name):
                return
            try:
                data = json.loads(self.manager.usage_cache_path().read_text())
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[name] = {"buckets": buckets, "fetchedAt": fetched_at, "identity": label, "resetCredits": reset_credits}
            atomic_json(self.manager.usage_cache_path(), data)

    def account_usage(self, name, home, offline=False, disabled=False, max_age=None):
        label = self.hooks.identity(home)
        row = {"name": name, "identity": label, "status": "offline", "buckets": [], "resetCredits": None, "fetchedAt": None, "disabled": disabled, "cached": False}
        if disabled:
            row["status"] = "disabled"
        elif label in ("not signed in", "unreadable auth cache"):
            row["status"] = label
        elif label == "API key":
            row["status"] = "API key: subscription quota not available"
        elif (offline or max_age) and self.manager.auth_failure(name, label) is not None:
            # The usage service rejected this login and nothing has cleared it: cached and
            # offline views say so without spawning Codex and without reusing an older cache
            # entry. A live read (no --cached) still tries again; a success clears the record.
            row["status"] = AUTH_FAILED_STATUS
        elif not offline:
            cached = self.manager.cached_usage(name, label, max_age)
            if cached is not None:
                row.update(status="ok (cached)", buckets=cached["buckets"], fetchedAt=cached["fetchedAt"], resetCredits=cached["resetCredits"], cached=True)
            else:
                try:
                    self.hooks.check_file_store(home)
                    response = self.hooks.read_limits(self.manager.codex(), self.manager.env(home))
                    buckets, fetched_at = normalize_limits(response), time.time()
                    row.update(status="ok", buckets=buckets, fetchedAt=fetched_at,
                               resetCredits=normalize_reset_credits(response))
                    self.manager.remember_usage(name, buckets, fetched_at, label, row["resetCredits"])
                    self.manager.clear_auth_failure(name)
                except (UsageError, SwapError) as error:
                    row["status"] = f"usage unavailable: {error}"
                    if is_sign_in_failure(error):
                        self.manager.remember_auth_failure(name, label)
                except OSError:
                    row["status"] = "usage unavailable: cannot start Codex CLI"
        return row

    def account_rows(self, name=None, offline=False, max_age=None):
        data = self.manager.read()
        enabled_names = {n for n, _ in self.manager.enabled_accounts()}
        if name is not None:
            selected, home = self.manager.account(name)
            accounts = [(selected, home, selected not in enabled_names)]
        else:
            accounts = [(key, Path(value["home"]), key not in enabled_names) for key, value in data["accounts"].items()]
        # Every server has its own account home; no global authentication switch is needed.
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(accounts)))) as pool:
            rows = list(pool.map(lambda item: self.manager.account_usage(item[0], item[1], offline=offline, disabled=item[2], max_age=max_age), accounts))
        slots = {key: index for index, key in enumerate(data["accounts"], 1)}
        for row in rows:
            row["slot"] = slots[row["name"]]
            row["active"] = row["name"] == data["active"]
        return rows

    def show_accounts(self, name=None, offline=False, json_output=False, include_spark=False, details=False, short=False, max_age=None, lang="en"):
        if short and json_output:
            raise SwapError("--short and --json are mutually exclusive.")
        rows = self.manager.account_rows(name, offline, max_age)
        if short:
            # An explicit `usage NAME --short` always shows that one account, even if
            # disabled; the aggregate `list --short` omits disabled accounts instead.
            print(short_line(rows if name is not None else [r for r in rows if not r.get("disabled")]))
            return rows
        if json_output:
            print(json.dumps(accounts_payload(rows), ensure_ascii=False, indent=2))
            return rows
        from xswap.display import render
        from xswap.codex_cli import bridge_hints, status_data
        # The text dashboard is a sweep point; --json/--short above stay read-only.
        state = status_data(self.manager)
        print(render(rows, state, include_spark, details, lang=lang, sessions=state["sessions"],
                     hints=bridge_hints(self.manager, state, __version__)))
        return rows

    def best_account(self, model=None, exclude=(), max_age=None):
        """Pick the signed-in account with the most codex headroom right now.

        A one-shot choice for launchers (like `codex exec`) that have no
        `--remote` hook and so cannot be protected by the live auto bridge.
        """
        # Reuse the single parallel-fetch implementation; rank_candidates drops disabled/excluded rows.
        return rank_candidates(self.manager.account_rows(max_age=max_age), model, exclude)
