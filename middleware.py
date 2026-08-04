import logging
import os
import re

from auth import reset_api_key, set_api_key
from oauth import validate_and_extract
from token_check import is_token_valid_remote

logger = logging.getLogger(__name__)

# Some MCP clients (e.g. Claude.ai custom connectors) send the endpoint as /MCP
# instead of /mcp. Routing is case-sensitive, so without this they'd get a 404.
# Mirrors the Java McpPathCaseNormalizationFilter (ES5MPTM3-6473).
_MCP_PATH_RE = re.compile(r"^/mcp(/.*)?$", re.IGNORECASE)

RESOURCE_METADATA_URL = os.environ.get(
    "OAUTH_BASE_URL", "https://mcp.metricool.ai"
) + "/.well-known/oauth-protected-resource"


class BearerAuthMiddleware:
    """
    Pure ASGI middleware that:
    1. Extracts the token from 'Authorization: Bearer <token>'.
    2. Validates it as a JWT (if it looks like one) using validate_and_extract().
    3. Stores the validated token in a ContextVar so tool functions can
       retrieve it via auth.get_api_key().
    4. Returns 401 with resource metadata URL for unauthenticated /mcp requests,
       so OAuth-capable clients (like mcp-remote) can discover the OAuth flow.

    Falls back to the MCP_API_KEY env var when no header is present
    (handled inside auth.get_api_key itself).
    """

    # Paths that do NOT require authentication. OAuth discovery must be public
    # in every form clients derive from the server URL, including the
    # /mcp/.well-known/* path-suffix variant (see oauth.OAUTH_ROUTES).
    _PUBLIC_PREFIXES = ("/.well-known/", "/mcp/.well-known/", "/health")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")

            # Normalize the case of /mcp paths before anything routes on them,
            # so /MCP behaves exactly like /mcp instead of falling through to a
            # 404. Done first so the public-prefix check below sees the
            # normalized path too.
            # Only the "/mcp" segment is lower-cased; anything below it is left
            # alone, since the rest of the path is case-sensitive by spec.
            if _MCP_PATH_RE.match(path) and not path.startswith("/mcp"):
                path = "/mcp" + path[4:]
                scope = {**scope, "path": path}
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes) and len(raw_path) >= 4:
                    scope["raw_path"] = b"/mcp" + raw_path[4:]

            # Let public endpoints through without auth
            if any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))
            auth_bytes = headers.get(b"authorization", b"")
            auth = auth_bytes.decode("utf-8", errors="ignore")
            method = scope.get("method", "?")

            if auth.lower().startswith("bearer "):
                raw_token = auth[7:].strip()
                try:
                    validated_token = validate_and_extract(raw_token)
                except ValueError as exc:
                    logger.warning("Token validation failed: %s", exc)
                    await _send_401(send, str(exc))
                    return

                # Probe Metricool API so an expired/revoked token surfaces as
                # 401+WWW-Authenticate at the HTTP layer (which lets OAuth
                # clients refresh) instead of as an opaque tool error inside a
                # 200 OK JSON-RPC response.
                if not await is_token_valid_remote(validated_token):
                    logger.warning("Token rejected by Metricool API: %s %s", method, path)
                    await _send_401(send, "Token has expired or been revoked.")
                    return

                token_ctx = set_api_key(validated_token)
                try:
                    await self.app(scope, receive, send)
                finally:
                    reset_api_key(token_ctx)
                return

            # No Bearer token — return 401 so clients discover the OAuth flow
            logger.warning("401 no-token: %s %s", method, path)
            await _send_401(send, "Bearer token required")
            return

        await self.app(scope, receive, send)


async def _send_401(send, detail: str) -> None:
    body = f'{{"error":"invalid_token","error_description":"{detail}"}}'.encode()
    www_auth = f'Bearer resource_metadata="{RESOURCE_METADATA_URL}"'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"www-authenticate", www_auth],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
