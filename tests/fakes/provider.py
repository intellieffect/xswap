"""A second platform, in memory: the shape a real one would have to take.

`FakeProvider` implements `xswap.providers.base.Provider` without touching a
disk, a subprocess or a network. It exists so the neutral half of xswap can be
tested against a platform that is *not* Codex -- which is the only way to prove
that `core` really stopped depending on one. Its quota bucket is deliberately
called something else, and its windows are deliberately not 300/10080.
"""
from __future__ import annotations

import time

from xswap.core.types import Check, CredentialState, Identity, QuotaShape, UsageSnapshot, UsageWindow
from xswap.providers import base

FAKE_QUOTA = QuotaShape(bucket_id="widget", short_minutes=60, weekly_minutes=4320)


def bucket(short=None, weekly=None, reached=None, bucket_id="widget", name="Widget"):
    """One normalized bucket in the shape a provider's `read_usage` produces."""
    windows = []
    if short is not None:
        windows.append({"position": "primary", "usedPercent": 100 - short, "remainingPercent": short,
                        "windowMinutes": FAKE_QUOTA.short_minutes, "resetsAt": 100.0})
    if weekly is not None:
        windows.append({"position": "secondary", "usedPercent": 100 - weekly, "remainingPercent": weekly,
                        "windowMinutes": FAKE_QUOTA.weekly_minutes, "resetsAt": 200.0})
    return {"id": bucket_id, "name": name, "plan": None, "windows": windows,
            "reached": reached, "credits": None}


def row(name, short=50, weekly=50, status="ok", disabled=False, active=False, reached=None):
    """One `Manager.account_rows()` row for this platform."""
    return {"name": name, "identity": f"{name}@example.test", "status": status,
            "buckets": [bucket(short, weekly, reached)], "resetCredits": None,
            "fetchedAt": 0.0, "disabled": disabled, "cached": False, "active": active}


class FakeProvider:
    """Everything `Provider` asks for, backed by dictionaries."""

    name = "fake"
    home_label = "Widget home"
    capabilities = frozenset({base.PER_ACCOUNT_HOME})
    quota_shape = FAKE_QUOTA

    def __init__(self, accounts=None, checks=()):
        # name -> {"label", "state", "short", "weekly", "reached"}
        self.accounts = dict(accounts or {})
        self.checks = list(checks)
        self.activated = []
        self.logins = []
        self.launched = []
        self.switched = []
        self.prepared = []

    # -- accounts --------------------------------------------------------------

    def identity(self, home):
        entry = self.accounts.get(str(home), {})
        label = entry.get("label", "not signed in")
        return Identity(label=label, stable_id=None if label == "not signed in" else label,
                        sentinel=label == "not signed in")

    def credential_state(self, home):
        entry = self.accounts.get(str(home), {})
        state = entry.get("state", CredentialState.OK)
        return CredentialState(state, "" if state == CredentialState.OK else state)

    def account_home(self, root, name):
        from xswap.core.paths import profile_dir
        return profile_dir(root, name) / "widget"

    def prepare_home(self, manager, home, source, accounts):
        self.prepared.append(str(home))

    # -- quota -----------------------------------------------------------------

    def read_usage(self, manager, home, env=None, timeout=None):
        entry = self.accounts.get(str(home), {})
        buckets = [bucket(entry.get("short", 50), entry.get("weekly", 50), entry.get("reached"))]
        windows = tuple(UsageWindow(key=("7d" if w["windowMinutes"] == FAKE_QUOTA.weekly_minutes else "1h"),
                                    used_percent=w["usedPercent"], remaining_percent=w["remainingPercent"],
                                    window_minutes=w["windowMinutes"], resets_at=w["resetsAt"])
                        for w in buckets[0]["windows"])
        return UsageSnapshot(account=entry.get("name", ""), windows=windows, buckets=buckets,
                             reset_credits=None, fetched_at=time.time(), shape=self.quota_shape,
                             extras={"widgets": 1})

    # -- selection and launching ----------------------------------------------

    def activate(self, manager, name):
        self.activated.append(name)
        return manager.use(name)

    def login(self, manager, name, **options):
        self.logins.append((name, options))

    def launch(self, manager, name, argv, *, auto_pool=None):
        self.launched.append((name, list(argv), auto_pool))
        return 0

    def switch_running(self, manager, name):
        self.switched.append(name)
        return {"applied": 0}

    # -- presentation ----------------------------------------------------------

    def status_data(self, manager, cleanup=True):
        return {"sessions": [], "enabled": False}

    def bridge_hints(self, manager, state, version):
        return []

    def session_hints(self, manager, target_version):
        return []

    def credit_lines(self, reset_credits):
        return []

    # -- diagnostics -----------------------------------------------------------

    def doctor_checks(self, manager):
        return [Check(name=name, status=status, detail=detail) for name, status, detail in self.checks]
