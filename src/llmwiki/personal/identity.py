"""The one personal user and the provider that hands it out. Reachable only from the personal entry point."""
from __future__ import annotations

import getpass
import secrets

from ..auth import User
from ..web.sessions import safe_eq

PERSONAL_SPACE = "personal"
PERSONAL_USER_ID = "me"
MIN_TOKEN_LEN = 22  # url-safe base64: 22 chars = 132 bits


def new_launch_token() -> str:
    return secrets.token_urlsafe(24)  # 192 bits


def os_user_name() -> str:
    try:
        return getpass.getuser() or "me"
    except Exception:  # noqa: BLE001 (no usable account name in some service/CI contexts)
        return "me"


def personal_user(name: str | None = None) -> User:
    return User(PERSONAL_USER_ID, name or os_user_name(), "personal", None, frozenset({PERSONAL_SPACE}), False)


class PersonalAuthProvider:
    """authenticate({"launch_token": t}) returns the personal user only for the per-launch secret (constant-time
    compare). `personal_only` makes WikiApp refuse this provider unless the personal entry point enabled it."""

    personal_only = True

    def __init__(self, launch_token: str, user: User | None = None):
        if not isinstance(launch_token, str) or len(launch_token) < MIN_TOKEN_LEN:
            raise ValueError("launch token must be at least 128 bits")
        self._token, self._user = launch_token, user or personal_user()

    def authenticate(self, credentials: dict) -> User | None:
        try:
            given = credentials.get("launch_token")
        except Exception:  # noqa: BLE001 (bad credentials never raise)
            return None
        if not isinstance(given, str) or not given or not safe_eq(given, self._token):
            return None
        return self._user

    def __repr__(self) -> str:
        return "PersonalAuthProvider(token=<hidden>)"
