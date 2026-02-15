from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from rest_framework import authentication, exceptions


@dataclass
class SimpleUser:
    """
    Lightweight user object backed by headers only.
    """

    id: str
    role: str

    @property
    def is_authenticated(self) -> bool:  # pragma: no cover - trivial
        return True


class HeaderUserAuthentication(authentication.BaseAuthentication):
    """
    Authenticate users via headers:

        X-User-Id: <string>
        X-User-Role: buyer | seller
    """

    header_user_id = "HTTP_X_USER_ID"
    header_user_role = "HTTP_X_USER_ROLE"

    def authenticate(self, request) -> Optional[Tuple[SimpleUser, None]]:
        user_id = request.META.get(self.header_user_id)
        role = request.META.get(self.header_user_role)

        if not user_id or not role:
            raise exceptions.AuthenticationFailed("Missing X-User-Id or X-User-Role header.")

        role = role.lower()
        if role not in {"buyer", "seller"}:
            raise exceptions.AuthenticationFailed("Invalid X-User-Role. Must be 'buyer' or 'seller'.")

        return SimpleUser(id=str(user_id), role=role), None

