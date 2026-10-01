"""Authentication plug-in point.

Every feature consumes only a `User`. Development uses DevAuthProvider; the SAML/AD
provider is implemented on-site (see docs/PLAN-company-llm-wiki.md section 2.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import Settings


@dataclass(frozen=True)
class User:
    id: str
    name: str
    department: str
    part: str | None = None
    # Spaces this user may read, e.g. {"dept-a", "dept-a/part-1"}. Mapped from AD groups via config.
    spaces: frozenset[str] = field(default_factory=frozenset)
    is_admin: bool = False


class AuthProvider(Protocol):
    def authenticate(self, credentials: dict) -> User | None:
        """Return the authenticated user, or None. Never raises on bad credentials."""


class DevAuthProvider:
    """Development stub: pick a user from a fixed table by id. NOT for production."""

    def __init__(self, users: dict[str, User]):
        self._users = users

    def authenticate(self, credentials: dict) -> User | None:
        return self._users.get(credentials.get("user_id", ""))


def get_provider(settings: Settings, dev_users: dict[str, User] | None = None) -> AuthProvider:
    if settings.auth_provider == "dev":
        if settings.env == "production":
            raise RuntimeError("Dev auth provider is forbidden when WIKI_ENV=production")
        return DevAuthProvider(dev_users or {})
    if settings.auth_provider == "saml":
        raise NotImplementedError("SAML provider is implemented on-site (AD FS)")
    raise ValueError(f"Unknown auth provider: {settings.auth_provider}")
