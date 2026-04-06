"""Authentication helpers for cua-house-server API."""

from __future__ import annotations

from fastapi import HTTPException, status


def require_auth(authorization: str | None, *, expected_token: str | None) -> None:
    """Validate bearer token.  No-op when no token is configured."""
    if not expected_token:
        return
    if authorization != f"Bearer {expected_token}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
