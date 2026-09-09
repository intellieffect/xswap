"""Read and clear stale OpenClaw auth-profile cooldowns in its state sqlite DB.

OpenClaw marks an OpenAI OAuth auth profile as blocked after a single 429 and
never re-probes it on its own (``usageStats[profileId].blockedUntil``); this
module lets xswap notice that condition (read-only) and, on request, clear
just that lock the same way manual recovery does: back up the database file,
rewrite only the affected keys, and let OpenClaw's own Gateway pick the change
up on restart. Nothing here starts or stops the Gateway -- that is the
caller's job (see ``Manager.clear_openclaw_cooldown`` in codex_swap.py) so a
failed stop can abort before any write happens.

Uses only the stdlib ``sqlite3`` module against OpenClaw's own on-disk shape;
never imports or shells out to OpenClaw. Every entry point refuses a
symlinked, non-owner, or group/other-accessible database file.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from xswap_credentials import CredentialError, read_auth
from xswap_live import jwt_claims

# The single machine-wide key OpenClaw's shared auth-profile store persists
# cooldown/order/lastGood state under (state_db table config_machine_state).
STATE_KEY = "authProfiles.state"


class OpenClawStateError(Exception):
    pass


def default_sqlite_path():
    """``$OPENCLAW_STATE_DIR/openclaw.sqlite`` or ``~/.openclaw/state/openclaw.sqlite``."""
    state_dir = os.environ.get("OPENCLAW_STATE_DIR")
    base = Path(state_dir).expanduser() if state_dir else Path.home() / ".openclaw" / "state"
    return (base / "openclaw.sqlite").resolve()


def profile_id_for_home(home):
    """The OpenClaw auth profile id xswap's bridge would use for this account
    ('openai:xswap-<hash>'), computed the same way xswap_bridge/openclaw.mjs's
    sourceCredential() does so both sides agree on the id. Returns None for an
    account with no usable ChatGPT OAuth credential (API key, unreadable, or
    missing an access/subject claim) rather than raising -- doctor and the
    cooldown clearer both treat that as "nothing to check", not an error.
    """
    try:
        data = read_auth(Path(home))
    except CredentialError:
        return None
    if not isinstance(data, dict) or data.get("auth_mode") == "apikey" or data.get("OPENAI_API_KEY"):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    claims = jwt_claims(access_token)
    auth_claim = claims.get("https://api.openai.com/auth")
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    account_id = tokens.get("account_id") or auth_claim.get("chatgpt_account_id")
    subject = claims.get("sub")
    if not isinstance(account_id, str) or not account_id or not isinstance(subject, str) or not subject:
        return None
    digest = hashlib.sha256(f"{account_id}\0{subject}".encode()).hexdigest()[:24]
    return f"openai:xswap-{digest}"


def human_delta(seconds):
    seconds = abs(seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def format_until(blocked_until_ms, now=None):
    """'<local clock> (in 6.0d)' / '... (6.0d ago)' for a millisecond timestamp."""
    now = time.time() if now is None else now
    remaining = blocked_until_ms / 1000 - now
    try:
        clock = datetime.fromtimestamp(blocked_until_ms / 1000).astimezone().strftime("%m/%d %H:%M %Z")
    except (ValueError, OverflowError, OSError):
        clock = "unknown time"
    return f"{clock} (in {human_delta(remaining)})" if remaining > 0 else f"{clock} ({human_delta(remaining)} ago)"


def _check_safe_file(path: Path):
    """Refuse a symlinked, non-regular, non-owner, or group/other-accessible file."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise OpenClawStateError(f"Cannot stat OpenClaw state database at {path}: {exc}") from None
    if stat.S_ISLNK(info.st_mode):
        raise OpenClawStateError(f"Refusing a symlinked OpenClaw state database: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise OpenClawStateError(f"Not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise OpenClawStateError(f"Refusing an OpenClaw state database not owned by the current user: {path}")
    if info.st_mode & 0o077:
        raise OpenClawStateError(f"Unsafe OpenClaw state database permissions at {path}: require mode 0600 (0400 also accepted).")


def _read_state(sqlite_path: Path):
    """Return the parsed authProfiles.state JSON object, or {} if the row/table is absent."""
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        try:
            cursor = conn.execute("SELECT value_json FROM config_machine_state WHERE state_key = ?", (STATE_KEY,))
        except sqlite3.OperationalError:
            return {}  # table absent: a fresh/foreign OpenClaw database, not an error
        row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    try:
        value = json.loads(row[0])
    except (ValueError, TypeError):
        raise OpenClawStateError("Invalid authProfiles.state JSON in OpenClaw's database.") from None
    return value if isinstance(value, dict) else {}


def read_cooldowns(sqlite_path, now=None):
    """{profileId: {blockedUntil, blockedReason, blockedSource, errorCount}} for every
    usageStats entry whose blockedUntil (epoch milliseconds) is still in the future.

    Read-only: opens the database with sqlite3's own ``mode=ro`` URI and never writes.
    A missing database file returns {} instead of raising, matching doctor's
    "OpenClaw or its DB is absent" skip case; an existing-but-unsafe file still raises.
    """
    sqlite_path = Path(sqlite_path)
    now = time.time() * 1000 if now is None else now
    if not sqlite_path.exists():
        return {}
    _check_safe_file(sqlite_path)
    state = _read_state(sqlite_path)
    usage_stats = state.get("usageStats")
    if not isinstance(usage_stats, dict):
        return {}
    result = {}
    for profile_id, stats in usage_stats.items():
        if not isinstance(stats, dict):
            continue
        blocked_until = stats.get("blockedUntil")
        if not isinstance(blocked_until, (int, float)) or isinstance(blocked_until, bool) or blocked_until <= now:
            continue
        result[profile_id] = {
            "blockedUntil": blocked_until,
            "blockedReason": stats.get("blockedReason"),
            "blockedSource": stats.get("blockedSource"),
            "errorCount": stats.get("errorCount"),
        }
    return result


def _make_backup(sqlite_path: Path, backup_dir: Path):
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = backup_dir.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise OpenClawStateError(f"Unsafe backup directory: {backup_dir}")
    os.chmod(backup_dir, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = backup_dir / f"openclaw-{stamp}-{os.getpid()}.sqlite"
    fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        # sqlite's online backup API copies committed pages including those still
        # in the WAL, which a raw file copy of the main database would miss.
        src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(backup_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.chmod(backup_path, 0o600)
    except BaseException:
        try:
            backup_path.unlink()
        except OSError:
            pass
        raise
    return backup_path


def clear_cooldown(sqlite_path, profile_ids, backup_dir):
    """Back up the database, then remove blockedUntil/blockedReason/blockedSource and
    zero errorCount for exactly the given profile ids; every other key (including other
    profiles' usageStats, order, lastGood) is left untouched. Returns the backup path.

    Refuses a missing, symlinked, non-owner, or group/other-accessible database, and
    raises rather than partially writing if the stored JSON is unreadable. The caller
    (``Manager.clear_openclaw_cooldown``) is responsible for stopping OpenClaw's Gateway
    first and starting it again after -- this function only ever touches the database.
    """
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        raise OpenClawStateError(f"OpenClaw state database not found: {sqlite_path}")
    _check_safe_file(sqlite_path)
    profile_ids = list(dict.fromkeys(pid for pid in profile_ids if pid))
    if not profile_ids:
        raise OpenClawStateError("No profile ids to clear.")

    backup_path = _make_backup(sqlite_path, backup_dir)

    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("SELECT value_json FROM config_machine_state WHERE state_key = ?", (STATE_KEY,))
        row = cursor.fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return backup_path
        try:
            state = json.loads(row[0])
        except (ValueError, TypeError):
            conn.execute("ROLLBACK")
            raise OpenClawStateError("Invalid authProfiles.state JSON in OpenClaw's database; refusing to write.") from None
        if not isinstance(state, dict):
            conn.execute("ROLLBACK")
            raise OpenClawStateError("Unexpected authProfiles.state shape in OpenClaw's database; refusing to write.")
        usage_stats = state.get("usageStats")
        if isinstance(usage_stats, dict):
            for profile_id in profile_ids:
                stats = usage_stats.get(profile_id)
                if not isinstance(stats, dict):
                    continue
                stats.pop("blockedUntil", None)
                stats.pop("blockedReason", None)
                stats.pop("blockedSource", None)
                stats["errorCount"] = 0
            state["usageStats"] = usage_stats
        conn.execute(
            "UPDATE config_machine_state SET value_json = ?, updated_at_ms = ? WHERE state_key = ?",
            (json.dumps(state), int(time.time() * 1000), STATE_KEY),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return backup_path
