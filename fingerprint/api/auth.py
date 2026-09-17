"""Optional API-key authentication.

When ``AUDIOFP_API_KEY`` is set, every request under ``/api/v1`` except the
health probe and the OpenAPI document must present the key, either as
``X-API-Key: <key>`` or as ``Authorization: Bearer <key>``.  Comparison is
constant-time.

Browsers cannot attach headers to ``<audio src=...>``, so the audio stream
endpoints additionally accept a **short-lived, track-scoped stream token**
(``?token=``) minted by ``GET /api/v1/tracks/<id>/stream-token``.  Tokens are
HMAC-signed with the API key, expire after :data:`STREAM_TOKEN_TTL` seconds and
only grant access to that one track, so a token that leaks into a proxy log or
browser history is worthless soon after and never reveals the key itself.

The bundled UI asks for the key once and stores it in the browser's local
storage.  For anything beyond a single shared secret (per-user keys, SSO,
rate limiting) put AudioFP behind a reverse proxy or API gateway - see
``docs/DEPLOYMENT.md``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time

from flask import Request

from ..utils.exceptions import AuthenticationError

PUBLIC_PATHS = frozenset({"/api/v1/health", "/api/v1/openapi.json"})

# GET /api/v1/tracks/<track_id>/audio  or  .../play  (and the deprecated /songs alias)
STREAM_PATH = re.compile(r"^/api/v1/(?:tracks|songs)/([^/]+)/(?:audio|play)$")
STREAM_TOKEN_TTL = 3600  # seconds


def extract_key(request: Request) -> str | None:
    header = request.headers.get("X-API-Key")
    if header:
        return header.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _signature(api_key: str, track_id: str, expires: int) -> str:
    message = f"{track_id}:{expires}".encode()
    return hmac.new(api_key.encode("utf-8"), message, hashlib.sha256).hexdigest()[:40]


def make_stream_token(api_key: str, track_id: str, ttl: int = STREAM_TOKEN_TTL) -> tuple[str, int]:
    """Return ``(token, expires_at)`` for streaming *track_id*."""
    expires = int(time.time()) + int(ttl)
    raw = f"{expires}.{_signature(api_key, track_id, expires)}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("="), expires


def verify_stream_token(api_key: str, track_id: str, token: str) -> bool:
    try:
        padded = token + "=" * (-len(token) % 4)
        expires_str, sig = base64.urlsafe_b64decode(padded.encode()).decode().split(".", 1)
        expires = int(expires_str)
    except (ValueError, UnicodeDecodeError):
        return False
    if expires < time.time():
        return False
    return hmac.compare_digest(sig, _signature(api_key, track_id, expires))


def check_request(request: Request, api_key: str) -> None:
    """Raise :class:`AuthenticationError` if *request* is not authorised."""
    if not api_key:
        return
    if request.path in PUBLIC_PATHS or request.method == "OPTIONS":
        return
    supplied = extract_key(request)
    if supplied is None and request.method == "GET":
        match = STREAM_PATH.match(request.path)
        token = request.args.get("token")
        if match and token:
            if verify_stream_token(api_key, match.group(1), token):
                return
            raise AuthenticationError("The stream token is invalid or has expired; request a new one from /stream-token.")
    if not supplied:
        raise AuthenticationError(
            "This server requires an API key. Send it as the X-API-Key header or as 'Authorization: Bearer <key>'.",
            details={"hint": "The key is the value of AUDIOFP_API_KEY on the server."},
        )
    if not hmac.compare_digest(supplied.encode("utf-8"), api_key.encode("utf-8")):
        raise AuthenticationError("Invalid API key.")
