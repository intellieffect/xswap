"""The neutral vocabulary core and providers share.

These are the only shapes that cross the boundary. A provider builds them out
of whatever its platform actually returns; core reads them without ever knowing
which platform that was. Every one is a frozen dataclass so a snapshot handed to
`ranking` or `tick` cannot be edited behind the caller's back.

`extras` on `UsageSnapshot` is the escape hatch for data only one platform has
(a platform's side quota buckets, its purchased credits): core carries it and shows
it where a provider asked for it, and never branches on its contents.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# A window at or above this many minutes is the long ("weekly") one when no
# provider said otherwise. Used only by `QuotaShape.is_weekly` and the two
# callers that classify a window by duration, never as a
# stand-in for a provider's real window lengths.
DAY_MINUTES = 1440

# The surfaces a provider may implement. Core consults these by name; a
# capability is present only when the provider can actually do it on this
# machine's install. `xswap.providers.base` re-exports them.
LIVE_SWITCH = "live_switch"          # can move a *running* session to another account
DESKTOP_APP = "desktop_app"          # has a desktop application xswap can launch per account
PATH_WRAPPER = "path_wrapper"        # owns a command on PATH that xswap can wrap
PER_ACCOUNT_HOME = "per_account_home"  # each account is a directory xswap creates and owns
OPENCLAW_SYNC = "openclaw_sync"      # its logins can be pushed into a local OpenClaw install

CAPABILITIES = frozenset({LIVE_SWITCH, DESKTOP_APP, PATH_WRAPPER, PER_ACCOUNT_HOME, OPENCLAW_SYNC})



@dataclass(frozen=True)
class QuotaShape:
    """Where a platform's subscription quota sits inside a normalized snapshot.

    `bucket_id` is the bucket that carries the quota a switch is decided on;
    `short_minutes` and `weekly_minutes` are that platform's two rolling
    windows. All three may be None, which means "work it out from the data":
    the first non-extra bucket (see `xswap.core.quota`), and windows by duration against
    `DAY_MINUTES`. That fallback exists so a pure helper stays callable with
    nothing but rows (the test suite does exactly that); production paths get
    the real shape from `Manager.provider.quota_shape`.
    """

    bucket_id: str | None = None
    short_minutes: int | None = None
    weekly_minutes: int | None = None

    def is_weekly(self, window):
        """True when WINDOW is this platform's long window."""
        minutes = window.get("windowMinutes")
        if self.weekly_minutes is not None:
            return minutes == self.weekly_minutes
        if minutes is None:
            return window.get("position") == "secondary"
        return minutes >= DAY_MINUTES


@dataclass(frozen=True)
class UsageWindow:
    """One rolling quota window: how much is left and when it resets."""

    key: str
    used_percent: float | None = None
    remaining_percent: float | None = None
    window_minutes: int | None = None
    resets_at: float | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    """One account's quota at one moment, as a provider reports it.

    `buckets` is the wire-shaped normalized payload the rest of xswap has always
    stored in `usage-cache.json` and printed from; `windows` is the flattened,
    typed view of the quota bucket that `ranking` reasons over. `shape` says
    which bucket and window lengths those are, so a consumer never has to guess.
    """

    account: str = ""
    windows: tuple[UsageWindow, ...] = ()
    buckets: list = field(default_factory=list)
    reset_credits: object = None
    fetched_at: float | None = None
    shape: QuotaShape = QuotaShape()
    extras: dict = field(default_factory=dict)

    def window(self, key):
        return next((w for w in self.windows if w.key == key), None)


@dataclass(frozen=True)
class Identity:
    """A local, unverified label for the login in one account home.

    `stable_id` is what two accounts must share to count as the same login;
    `label` is what the user sees; `org` is the organization the platform
    reports, when it has one. `sentinel` carries the non-address answers
    ("not signed in", "unreadable auth cache", ...) unchanged.
    """

    label: str
    stable_id: str | None = None
    org: str | None = None
    sentinel: bool = False


@dataclass(frozen=True)
class CredentialState:
    """Whether the credentials in one account home can be used right now."""

    state: str  # "ok" | "sign_in_required" | "unreadable"
    reason: str = ""

    OK = "ok"
    SIGN_IN_REQUIRED = "sign_in_required"
    UNREADABLE = "unreadable"

    @property
    def ok(self):
        return self.state == self.OK


@dataclass(frozen=True)
class Check:
    """One read-only diagnostic result: `xswap doctor`'s unit of output."""

    name: str
    status: str
    detail: str = ""

    def as_dict(self):
        return {"name": self.name, "status": self.status, "detail": self.detail}

    @classmethod
    def from_dict(cls, value):
        return cls(name=value["name"], status=value["status"], detail=value.get("detail", ""))
