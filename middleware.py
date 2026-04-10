import logging
import os

from auth import reset_api_key, set_api_key
from oauth import validate_and_extract

logger = logging.getLogger(__name__)

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

    # Paths that do NOT require authentication
    _PUBLIC_PREFIXES = ("/.well-known/", "/health")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")

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
