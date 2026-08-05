"""
JWT authentication for the Metadata Service.

Provides:
  - create_access_token()    — sign a JWT with user claims
  - decode_token()           — framework-agnostic JWT validation (FastAPI + gRPC)
  - get_current_user()       — FastAPI dependency that validates Bearer tokens
  - hash_password()          — bcrypt password hashing
  - verify_password()        — bcrypt password verification
  - require_service_key()    — FastAPI dependency for service-to-service calls
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import settings

# ─── Password hashing ─────────────────────────────────────────────────────────

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ─── JWT ──────────────────────────────────────────────────────────────────────

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)


def create_access_token(user_id: str, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_expire_minutes
    )
    payload = {
        "sub": user_id,
        "email": email,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


class AuthError(Exception):
    """Raised by decode_token() when a bearer token is missing or invalid."""


def decode_token(token: Optional[str]) -> dict:
    """
    Validate a JWT and return {"user_id": ..., "email": ...}.
    Framework-agnostic — used by both the FastAPI dependency below and the
    gRPC auth interceptor (metadata_service/app/grpc_auth.py).
    Raises AuthError if the token is missing, malformed, or expired.
    """
    if not token:
        raise AuthError("missing token")
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError:
        raise AuthError("invalid or expired token")
    user_id: Optional[str] = payload.get("sub")
    if user_id is None:
        raise AuthError("token missing subject claim")
    return {"user_id": user_id, "email": payload.get("email")}


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """
    FastAPI dependency — validates a Bearer JWT and returns user claims.
    Inject into route handlers:  user: dict = Depends(get_current_user)
    """
    try:
        return decode_token(token)
    except AuthError:
        raise _CREDENTIALS_EXCEPTION


# ─── Service-to-service API key ───────────────────────────────────────────────


async def require_service_key(
    x_service_key: Optional[str] = Header(None),
) -> None:
    """
    FastAPI dependency for internal service endpoints.
    Skipped entirely when SERVICE_API_KEY is not configured (dev / test mode).
    """
    if not settings.service_api_key:
        return  # API key enforcement disabled
    if x_service_key != settings.service_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing service API key",
        )
