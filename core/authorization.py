from __future__ import annotations

from core.errors import AuthenticationFailure
from core.identity import JurisUser, RequestIdentity


def require_verified_user(identity: RequestIdentity | None) -> JurisUser:
    if identity is None or not identity.user.is_verified:
        raise AuthenticationFailure("A verified authenticated identity is required.")
    if not identity.user.user_id.strip():
        raise AuthenticationFailure("Authenticated identity is invalid.")
    return identity.user
