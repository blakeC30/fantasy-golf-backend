"""
Authentication service.

Handles the two concerns that don't belong in a route handler:
  1. Password hashing and verification (bcrypt)
  2. JWT creation and decoding (python-jose)
  3. Google ID token verification (google-auth)

Keeping this in a service makes it easy to unit-test without starting a web server.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt. The salt is embedded in the returned string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if the plaintext password matches the stored bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    """
    Create a short-lived JWT access token (15 minutes).

    The 'type' claim distinguishes access tokens from refresh tokens so that a
    refresh token cannot be used as an access token and vice versa.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": user_id, "exp": expire, "type": "access"}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """Create a long-lived JWT refresh token (7 days), stored in an httpOnly cookie."""
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {"sub": user_id, "exp": expire, "type": "refresh"}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.

    Raises JWTError if the token is expired, malformed, or is not an access token.
    The caller (dependencies.py) converts JWTError into an HTTP 401 response.
    """
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != "access":
        raise JWTError("Token is not an access token")
    return payload


def decode_refresh_token(token: str) -> dict:
    """Decode and validate a JWT refresh token. Raises JWTError on failure."""
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("type") != "refresh":
        raise JWTError("Token is not a refresh token")
    return payload


def verify_google_id_token(id_token: str) -> dict:
    """
    Verify a Google-issued ID token and return its claims.

    The google-auth library makes a network call to Google's public key endpoint
    the first time, then caches the keys. Returns a dict with fields like:
      - 'sub': Google user ID (stable, use this as the google_id)
      - 'email': user's email
      - 'name': user's full name

    Raises google.auth.exceptions.GoogleAuthError if the token is invalid.
    GOOGLE_CLIENT_ID must match the client ID used in the frontend.
    """
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        id_token,
        google_requests.Request(),
        settings.GOOGLE_CLIENT_ID,
    )
