import os
from contextvars import ContextVar

_current_api_key: ContextVar[str] = ContextVar("current_api_key", default="")


def set_api_key(key: str) -> object:
    """Set API key for current request context. Returns a token for reset."""
    return _current_api_key.set(key)


def reset_api_key(token: object) -> None:
    _current_api_key.reset(token)


def get_api_key() -> str:
    """
    Returns the Metricool API key for the current request.

    Priority:
    1. Authorization: Bearer <key> header (set by BearerAuthMiddleware)
    2. MCP_API_KEY environment variable (single-user / local mode)
    """
    key = _current_api_key.get()
    if not key:
        key = os.environ.get("MCP_API_KEY", "")
    if not key:
        raise ValueError(
            "Missing Metricool API key. "
            "Provide 'Authorization: Bearer <key>' header or set the MCP_API_KEY env var."
        )
    return key
