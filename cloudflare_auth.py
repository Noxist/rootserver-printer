"""Cloudflare Access assertion validation for printer administrator routes."""

from __future__ import annotations

import logging
import threading
import time

import httpx
import jwt


_CACHE_TTL = 3600
_MAX_STALE = 86400
_cache = {"keys": [], "fetched_at": 0.0, "expires_at": 0.0}
_lock = threading.Lock()


def _issuer(team: str) -> str:
    clean = (team or "").strip().rstrip("/")
    if clean.startswith("https://"):
        clean = clean[8:]
    elif clean.startswith("http://"):
        clean = clean[7:]
    if clean.endswith(".cloudflareaccess.com"):
        return f"https://{clean}"
    return f"https://{clean}.cloudflareaccess.com"


def _keys(team: str) -> list[dict]:
    now = time.monotonic()
    with _lock:
        if _cache["keys"] and now < _cache["expires_at"]:
            return list(_cache["keys"])
        try:
            response = httpx.get(
                f"{_issuer(team)}/cdn-cgi/access/certs",
                timeout=5.0,
                follow_redirects=False,
            )
            response.raise_for_status()
            if len(response.content) > 256 * 1024:
                raise ValueError("oversized JWKS")
            keys = response.json().get("keys", [])
            if not isinstance(keys, list) or not 1 <= len(keys) <= 20:
                raise ValueError("invalid JWKS")
            _cache.update(
                keys=keys,
                fetched_at=now,
                expires_at=now + _CACHE_TTL,
            )
        except Exception:
            logging.error("Unable to refresh Cloudflare Access signing keys")
            if now - _cache["fetched_at"] > _MAX_STALE:
                return []
        return list(_cache["keys"])


def validate_access_assertion(token: str, team: str, audience: str | list[str] | set[str] | tuple[str, ...]) -> dict | None:
    if not token or not team or not audience or len(token) > 16384:
        return None
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            return None
    except (jwt.InvalidTokenError, Exception):
        return None

    key_data = next(
        (item for item in _keys(team) if item.get("kid") == header["kid"]),
        None,
    )
    if not key_data:
        return None
    try:
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        aud = list(audience) if isinstance(audience, (list, set, tuple)) else audience
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=_issuer(team),
            audience=aud,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return None
