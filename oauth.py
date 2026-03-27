"""
OAuth 2.0 discovery endpoints and JWT validation.

Mirrors the Java remote-server OAuth implementation:
  - OAuthResource.java          → /.well-known/* endpoints
  - OAuthProtectedResourceMetadata.java
  - OAuthAuthorizationServerMetadata.java
  - MetricoolJwtDecoder.java    → HMAC-SHA JWT validation

The MCP server does NOT run an OAuth server itself.
It acts as a protected resource that points clients to
app.metricool.com for authorization/token exchange.

Required environment variables:
  JWT_SECRET   HMAC secret shared with Metricool's OAuth server (mandatory in production)

Optional environment variables (defaults match production values):
  OAUTH_ISSUER                   default: https://ai.metricool.com
  OAUTH_BASE_URL                 default: https://mcp.metricool.ai
  OAUTH_AUTHORIZATION_ENDPOINT   default: https://app.metricool.com/oauth/authorize
  OAUTH_TOKEN_ENDPOINT           default: https://app.metricool.com/oauth/token
  OAUTH_REGISTRATION_ENDPOINT    default: https://app.metricool.com/oauth/register
"""

import logging
import os

import jwt
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
JWT_SECRET = os.environ.get("JWT_SECRET", "")

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
            "resource_documentation": "",
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
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
            "code_challenge_methods_supported": ["S256", "plain"],
        }
    )


# Starlette Route objects — mounted into the main app in server.py
OAUTH_ROUTES = [
    Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
    Route("/.well-known/oauth-authorization-server", oauth_authorization_server_metadata),
    Route("/.well-known/openid-configuration", oauth_authorization_server_metadata),
]

# ---------------------------------------------------------------------------
# JWT validation  (mirrors MetricoolJwtDecoder.java)
# ---------------------------------------------------------------------------


def is_jwt(token: str) -> bool:
    """Return True if the token looks like a JWT (three base64 segments separated by dots)."""
    parts = token.split(".")
    return len(parts) == 3


def validate_and_extract(token: str) -> str:
    """
    Validate a JWT and return the raw token so it can be forwarded as
    Authorization: Bearer to the Metricool API.

    Supports two token types:
    1. HS256 JWTs signed with JWT_SECRET (legacy / internal tokens)
    2. OAuth access tokens signed by the authorization server (RS256, etc.)
       — these are passed through; the Metricool API validates them.

    Raises ValueError with a user-friendly message on failure.

    If JWT_SECRET is not configured (local/dev mode) the token is trusted as-is.
    """
    if not is_jwt(token):
        # Plain API key — no JWT validation needed
        return token

    if not JWT_SECRET:
        logger.warning(
            "JWT_SECRET not set — skipping JWT signature validation. "
            "Set JWT_SECRET in production."
        )
        return token

    # Peek at the algorithm to decide validation strategy
    try:
        header = jwt.get_unverified_header(token)
    except jwt.DecodeError:
        raise ValueError("Token is malformed.")

    alg = header.get("alg", "")

    if alg == "HS256":
        # Internal token — validate locally with shared secret
        try:
            jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            logger.debug("HS256 JWT validated successfully")
            return token
        except jwt.ExpiredSignatureError:
            raise ValueError("Token has expired.")
        except jwt.InvalidSignatureError:
            raise ValueError("Token signature is invalid.")
        except jwt.DecodeError:
            raise ValueError("Token is malformed.")
        except jwt.InvalidTokenError as exc:
            raise ValueError(f"Invalid token: {exc}")
    else:
        # OAuth token (RS256, etc.) — pass through, the Metricool API validates it
        logger.debug("OAuth JWT (alg=%s) accepted, forwarding to API", alg)
        return token
