"""Local, unverified labels for a Codex home, plus the credential-store guard.

Every function here reads one home directory and nothing else -- no `Manager`,
no registry -- so the collaborators and the facade can share them.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import tomllib

from xswap.credentials import CredentialError, read_auth
from xswap.errors import SwapError
from xswap.live import jwt_claims


def check_file_store(home):
    config = home / "config.toml"
    try:
        data = tomllib.loads(config.read_text()) if config.exists() else {}
    except (ValueError, OSError):
        raise SwapError(f"Cannot read valid config.toml at {home}") from None
    store = data.get("cli_auth_credentials_store", "file")
    if store != "file":
        raise SwapError(f"{home}: credential storage is {store!r}; this version supports file storage only. No credentials changed.")


def chatgpt_org_id(home, name):
    """Unverified org id from a local credential, for the same-organization pool guard.

    Checks access_token first, matching xswap_live.load_credentials' claim source; the
    id_token fallback is this guard's own extension (load_credentials has none), covering
    a credential whose access_token lacks the claim but whose id_token still carries it.
    Fails closed: raises SwapError instead of returning None/unknown, because an
    undeterminable organization must never be treated as matching another account's
    organization (that would silently let mismatched orgs share one pool).
    """
    try:
        data = read_auth(home)
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            for key in ("access_token", "id_token"):
                token = tokens.get(key)
                if isinstance(token, str) and token:
                    auth = jwt_claims(token).get("https://api.openai.com/auth")
                    if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
                        return auth["chatgpt_account_id"]
    except CredentialError:
        pass
    raise SwapError(f"cannot verify the ChatGPT organization for account {name}; run xswap login {name} or pass --allow-mixed.")


def identity(home):
    path = Path(home) / "auth.json"
    if not path.exists():
        return "not signed in"
    try:
        data = read_auth(home)
        if not isinstance(data, dict):
            return "unreadable auth cache"
        if data.get("auth_mode") == "apikey" or data.get("OPENAI_API_KEY"):
            return "API key"
        tokens = data.get("tokens") or {}
        if not isinstance(tokens, dict):
            return "unreadable auth cache"
        token = tokens.get("id_token", "")
        if not isinstance(token, str):
            return "unreadable auth cache"
        parts = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))) if len(parts) == 3 else {}
        # These are unverified local labels, not evidence that the login is valid.
        label = (claims.get("email") or "ChatGPT") if isinstance(claims, dict) else "ChatGPT"
        return "".join(c for c in str(label) if c.isprintable()) if tokens.get("access_token") else "not signed in"
    except CredentialError as exc:
        raise SwapError(str(exc)) from None
    except (ValueError, OSError, IndexError, TypeError):
        return "unreadable auth cache"
