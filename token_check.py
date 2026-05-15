"""
Remote token revocation check against the Metricool API.

Local expiry detection lives in oauth.validate_and_extract — that handles the
common case (token reached its 'exp'). This module catches the rarer case of
tokens that were revoked or invalidated server-side before their stated
expiry, so the middleware can still return 401+WWW-Authenticate and let
OAuth-aware clients refresh.

Strategy: probe a cheap, authenticated endpoint (/api/v2/settings/brands).
Only **negative** results are cached — and only briefly. We deliberately do
NOT cache successful probes: with local 'exp' checking already in place the
upstream cost is one probe per request, and a stale "valid" entry is exactly
what previously let expired tokens reach the tool layer (returning a JSON-RPC
isError response that clients like ChatGPT do not translate into a refresh).

Network errors during the probe fail open (treat as valid) so a flaky
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
# Short negative-cache TTL: just long enough to absorb a burst of tool calls
# from the same expired/revoked token without hammering the API. Long enough
# to matter, short enough that a freshly refreshed token starts working
# quickly without a server restart.
_NEGATIVE_TTL_SECONDS = 10.0
_MAX_ENTRIES = 1000
_PROBE_TIMEOUT = 5

# Cache holds ONLY known-invalid tokens. Valid tokens are never cached so
# revocation that happens mid-session is picked up on the next request.
_invalid_cache: dict[str, float] = {}
_lock = threading.Lock()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_known_invalid(token: str) -> bool:
    h = _hash(token)
    now = time.monotonic()
    with _lock:
        expiry = _invalid_cache.get(h)
        return expiry is not None and expiry > now


def _mark_invalid(token: str) -> None:
    h = _hash(token)
    now = time.monotonic()
    with _lock:
        _invalid_cache[h] = now + _NEGATIVE_TTL_SECONDS
        if len(_invalid_cache) > _MAX_ENTRIES:
            stale = [k for k, exp in _invalid_cache.items() if exp < now]
            for k in stale:
                _invalid_cache.pop(k, None)


def _probe_sync(token: str) -> bool | None:
    """Run the probe in a worker thread.

    Returns:
      True   — API accepted the token
      False  — API rejected with 401/403 (token revoked or otherwise invalid)
      None   — network error; caller should fail open without caching
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
        return None

    valid = resp.status_code not in (401, 403)
    logger.debug("Token probe: status=%d valid=%s", resp.status_code, valid)
    return valid


async def is_token_valid_remote(token: str) -> bool:
    """Return False if the Metricool API has rejected this token recently.

    Negative-only caching: a fresh 401 from the API short-circuits subsequent
    requests for a few seconds (so a burst of tool calls all surface as
    401+WWW-Authenticate instead of repeatedly probing). Positive results are
    not cached — local 'exp' checking in oauth.validate_and_extract already
    blocks most expired tokens, and we want revocation detected on the very
    next request.
    """
    if _is_known_invalid(token):
        return False
    result = await anyio.to_thread.run_sync(_probe_sync, token)
    if result is False:
        _mark_invalid(token)
        return False
    # True or None (network error → fail open)
    return True
