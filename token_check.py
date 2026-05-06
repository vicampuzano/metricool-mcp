"""
Remote token validation against the Metricool API, with short-TTL caching.

Why this exists: Metricool's access tokens carry a non-decodable (likely
compressed) JWT payload, so the resource server cannot read the 'exp' claim
locally to know when to return 401+WWW-Authenticate. Without that signal,
OAuth-aware MCP clients (Claude.ai, mcp-remote) never trigger their
refresh_token flow and the user must reconnect manually.

Strategy: probe a cheap, authenticated endpoint (/api/v2/settings/brands)
once per token every TTL seconds. The probe result is cached so normal
request traffic only pays the network cost once a minute.

Network errors during the probe fail open (treat token as valid) so a flaky
upstream doesn't break legitimate traffic; the API will reject on the real
call if the token is actually bad.
"""

import hashlib
import logging
import os
import threading
import time

import anyio
import requests

from oauth import is_jwt

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("METRICOOL_BASE_URL", "https://app.metricool.com").rstrip("/")
_PROBE_PATH = "/api/v2/settings/brands"
_TTL_SECONDS = 60.0
_MAX_ENTRIES = 1000
_PROBE_TIMEOUT = 5

_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_lookup(token: str) -> bool | None:
    """Return cached validity, or None if no fresh entry exists."""
    h = _hash(token)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(h)
        if cached and cached[0] > now:
            return cached[1]
    return None


def _cache_store(token: str, valid: bool) -> None:
    h = _hash(token)
    now = time.monotonic()
    with _lock:
        _cache[h] = (now + _TTL_SECONDS, valid)
        if len(_cache) > _MAX_ENTRIES:
            expired = [k for k, (exp, _) in _cache.items() if exp < now]
            for k in expired:
                _cache.pop(k, None)


def _probe_sync(token: str) -> tuple[bool, bool]:
    """Run the probe in a worker thread. Returns (is_valid, is_definitive).

    is_definitive is False on network errors so the caller knows not to cache
    the fail-open True — we want to retry on the next request, not lock in a
    transient hiccup for the full TTL.
    """
    headers = (
        {"Authorization": f"Bearer {token}"}
        if is_jwt(token)
        else {"X-Mc-Auth": token}
    )
    try:
        resp = requests.get(
            f"{_BASE_URL}{_PROBE_PATH}",
            headers=headers,
            params={"integrationSource": "MCP"},
            timeout=_PROBE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Token probe network error: %s — failing open", exc)
        return True, False

    valid = resp.status_code not in (401, 403)
    logger.debug("Token probe: status=%d valid=%s", resp.status_code, valid)
    return valid, True


async def is_token_valid_remote(token: str) -> bool:
    """Async-friendly wrapper: cache lookup first, probe in a thread on miss."""
    cached = _cache_lookup(token)
    if cached is not None:
        return cached
    valid, definitive = await anyio.to_thread.run_sync(_probe_sync, token)
    if definitive:
        _cache_store(token, valid)
    return valid
