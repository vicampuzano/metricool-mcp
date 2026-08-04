"""
OAuth 2.0 discovery endpoints and JWT expiry detection.

Mirrors the Java remote-server OAuth implementation:
  - OAuthResource.java          → /.well-known/* endpoints
  - OAuthProtectedResourceMetadata.java
  - OAuthAuthorizationServerMetadata.java
  - MetricoolJwtDecoder.java    → reads 'exp' to surface expired tokens as 401

Signature verification is delegated to the Metricool API (see
token_check.is_token_valid_remote and the upstream call inside the tools).

Optional environment variables (defaults match production values):
  OAUTH_ISSUER                   default: https://ai.metricool.com
  OAUTH_BASE_URL                 default: https://mcp.metricool.ai
  OAUTH_AUTHORIZATION_ENDPOINT   default: https://app.metricool.com/oauth/authorize
  OAUTH_TOKEN_ENDPOINT           default: https://app.metricool.com/oauth/token
  OAUTH_REGISTRATION_ENDPOINT    default: https://app.metricool.com/oauth/register
"""

import base64
import json
import logging
import os
import time
import zlib

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (mirrors application-production.yaml oauth.server.* values)
# ---------------------------------------------------------------------------

ISSUER = os.environ.get("OAUTH_ISSUER", "https://ai.metricool.com")
BASE_URL = os.environ.get("OAUTH_BASE_URL", "https://mcp.metricool.ai")
AUTHORIZATION_ENDPOINT = os.environ.get(
    "OAUTH_AUTHORIZATION_ENDPOINT", "https://app.metricool.com/oauth/authorize"
)
TOKEN_ENDPOINT = os.environ.get(
    "OAUTH_TOKEN_ENDPOINT", "https://app.metricool.com/oauth/token"
)
REGISTRATION_ENDPOINT = os.environ.get(
    "OAUTH_REGISTRATION_ENDPOINT", "https://app.metricool.com/oauth/register"
)

# ---------------------------------------------------------------------------
# Discovery endpoint handlers
# ---------------------------------------------------------------------------


async def oauth_protected_resource(request: Request) -> JSONResponse:
    """
    GET /.well-known/oauth-protected-resource
    RFC 8705 Protected Resource Metadata.
    Tells clients which authorization server issues tokens for this resource.
    """
    return JSONResponse(
        {
            "resource": f"{BASE_URL}/mcp",
            "authorization_servers": [ISSUER],
            "scopes_supported": ["mcp:read", "mcp:write"],
            "bearer_methods_supported": ["header"],
            "tls_client_certificate_bound_access_tokens": False,
            "resource_documentation": "https://metricool.com/integrations/mcp",
            "resource_policy_uri": "https://metricool.com/privacy-policy/",
            "resource_tos_uri": "https://metricool.com/legal-terms/",
        }
    )


async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    """
    GET /.well-known/oauth-authorization-server
    GET /.well-known/openid-configuration
    RFC 8414 Authorization Server Metadata.
    Points clients to app.metricool.com for the actual OAuth flow.
    """
    return JSONResponse(
        {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZATION_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "registration_endpoint": REGISTRATION_ENDPOINT,
            "scopes_supported": ["mcp:read", "mcp:write"],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query", "fragment"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            # client_secret_post is required by clients that cannot send HTTP
            # Basic credentials at the token endpoint (e.g. Mistral Le Chat),
            # which otherwise fail the token exchange. Mirrors Java ES5MPTM3-6464.
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "none",
            ],
            "code_challenge_methods_supported": ["S256", "plain"],
        }
    )


# Starlette Route objects — mounted into the main app in server.py.
#
# Besides the root forms, MCP clients derive the discovery URL from the MCP
# server URL (https://host/mcp) in two ways, and both must return the same
# document (Java ES5MPTM3-5817):
#   - RFC 8414 path-insertion:  /.well-known/<doc>/mcp
#   - path-suffix (MCP SDKs):   /mcp/.well-known/<doc>
# The /mcp/.well-known/* forms must also stay public — see the middleware's
# _PUBLIC_PREFIXES, which exempts them from the Bearer requirement.
_AS_METADATA_PATHS = [
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/openid-configuration/mcp",
    "/mcp/.well-known/oauth-authorization-server",
    "/mcp/.well-known/openid-configuration",
]

_PROTECTED_RESOURCE_PATHS = [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/mcp/.well-known/oauth-protected-resource",
]

OAUTH_ROUTES = [
    *[Route(p, oauth_protected_resource) for p in _PROTECTED_RESOURCE_PATHS],
    *[Route(p, oauth_authorization_server_metadata) for p in _AS_METADATA_PATHS],
]

# ---------------------------------------------------------------------------
# JWT expiry detection  (mirrors MetricoolJwtDecoder.java)
# ---------------------------------------------------------------------------


def is_jwt(token: str) -> bool:
    """Return True if the token looks like a JWT (three base64 segments separated by dots)."""
    parts = token.split(".")
    return len(parts) == 3


def _b64url_decode(segment: str) -> bytes:
    """Decode a JWT base64url segment, restoring stripped '=' padding."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _decode_jwt_claims(token: str) -> dict | None:
    """Return JWT claims dict or None if the payload is unreadable.

    Mirrors what jjwt does in MetricoolJwtDecoder: handles JWTs whose payload
    has been compressed (header "zip":"DEF" — Metricool's OAuth tokens use
    this). PyJWT 2.x does not support zip:DEF natively, so we decompress
    manually and then parse the JSON.

    Signature is NOT verified here — the Metricool API is the authority on
    signature validity. The only purpose of this decode is to read 'exp'
    locally so an expired token surfaces as 401+WWW-Authenticate before any
    tool runs (matching the Java behavior).
    """
    try:
        _, payload_seg, _ = token.split(".")
    except ValueError:
        return None
    try:
        raw = _b64url_decode(payload_seg)
    except (ValueError, TypeError):
        return None

    candidates: list[bytes] = [raw]
    # zlib-wrapped (header 0x78 ...) — what java.util.zip.Deflater emits by
    # default, and what jjwt's "DEF" codec produces.
    try:
        candidates.append(zlib.decompress(raw))
    except zlib.error:
        pass
    # raw DEFLATE (RFC 1951, no zlib wrapper) — what the JWT spec literally
    # specifies for "zip":"DEF". Some issuers honor that strictly.
    try:
        candidates.append(zlib.decompress(raw, -zlib.MAX_WBITS))
    except zlib.error:
        pass

    for data in candidates:
        try:
            obj = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def validate_and_extract(token: str) -> str:
    """
    Validate a JWT and return the raw token so it can be forwarded as
    Authorization: Bearer to the Metricool API.

    Strategy (mirrors Java's MetricoolJwtDecoder):
      1. If it doesn't look like a JWT (no dots), treat as a plain API key.
      2. Decode the payload locally (handling zip:DEF compression) and reject
         expired tokens with ValueError so the middleware can return 401 +
         WWW-Authenticate, letting OAuth-aware clients refresh.
      3. If the payload can't be decoded, pass through and let the Metricool
         API be the authority — token_check.is_token_valid_remote will probe
         it before the tool runs.

    Signature verification is delegated to the Metricool API: PyJWT cannot
    decompress zip:DEF payloads, and the shared secret stored in JWT_SECRET
    is not the same byte sequence Java's MetricoolSecretDecoder feeds to its
    HMAC primitive, so verifying locally would either reject valid tokens or
    require duplicating the XOR-mask trick. The API will reject any token
    with a bad signature anyway.
    """
    if not is_jwt(token):
        # Plain API key — no JWT validation needed
        return token

    claims = _decode_jwt_claims(token)
    if claims is None:
        # Payload not decodable even after trying zlib variants. Let the
        # remote probe in middleware decide; the API is the authority.
        logger.debug("JWT payload not decodable locally — forwarding to API")
        return token

    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp <= time.time():
        raise ValueError("Token has expired.")

    return token
