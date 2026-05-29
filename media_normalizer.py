"""
Media URL normalization.

Before a post is created/updated, media URLs that do NOT point to a trusted
Metricool host are pushed through the Metricool normalization endpoint so the
backend downloads the asset and re-hosts it. URLs already on a trusted host are
left untouched.

Mirrors the Java RestTemplateMediaUrlNormalizer behaviour (tickets
ES5MPTM3-5414 + ES5MPTM3-5544), adapted to this project's conventions:
  - media items are plain URL strings
  - the trusted-host list is configurable via the METRICOOL_NORMALIZED_HOSTS
    env var (comma-separated); defaults match the Java application.yaml
  - failures raise MediaNormalizationError, whose message is what the MCP
    client sees

This module only depends on the stdlib + a duck-typed client exposing
``normalize_image_url(url) -> str`` so it can be unit-tested in isolation.
"""

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hosts whose assets Metricool can already download directly — never renormalized.
DEFAULT_NORMALIZED_HOSTS = (
    "static.metricool.com",
    "metricool-download.s3.eu-west-1.amazonaws.com",
    "metricool-data.s3.eu-west-1.amazonaws.com",
)


def normalized_hosts() -> set[str]:
    """Trusted hosts, overridable via METRICOOL_NORMALIZED_HOSTS (CSV)."""
    raw = os.environ.get("METRICOOL_NORMALIZED_HOSTS")
    if raw:
        return {h.strip() for h in raw.split(",") if h.strip()}
    return set(DEFAULT_NORMALIZED_HOSTS)


class MediaNormalizationError(Exception):
    """A media URL could not be normalized by the Metricool backend.

    The message is the user-facing text surfaced to the MCP client (matches the
    Java MediaNormalizationFailedExceptionProcessor output).
    """

    def __init__(self, url: str, cause: Exception | None = None) -> None:
        self.url = url
        message = (
            f"Couldn't upload the media at '{url}' to Metricool. "
            "Make sure the link is publicly accessible. "
            "For Google Drive links, ensure the user has linked Google Drive in "
            "their Metricool account."
        )
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


def _host_of(url: str) -> str | None:
    """Extract the host of a URL, or None if it cannot be parsed."""
    try:
        return urlparse(url).hostname
    except (ValueError, AttributeError):
        return None


def is_already_normalized(url: str, hosts: set[str]) -> bool:
    """True if the URL's host is a trusted Metricool host.

    An unparseable URL is treated as NOT normalized (so it gets sent to the
    endpoint), mirroring the Java behaviour for invalid URIs.
    """
    host = _host_of(url)
    return host in hosts if host else False


def normalize_url(url: str, client, hosts: set[str]) -> str:
    """Normalize a single media URL, returning the (possibly new) URL.

    Trusted host -> returned unchanged. Otherwise the normalization endpoint is
    called and its trimmed text body replaces the URL. Auth errors
    (TokenInvalidError) propagate untouched; any other failure or an
    empty/blank body raises MediaNormalizationError.
    """
    # Non-string entries can't be normalized — leave them as-is.
    if not isinstance(url, str):
        return url

    if is_already_normalized(url, hosts):
        return url

    try:
        body = client.normalize_image_url(url)
    except Exception as exc:  # noqa: BLE001 — re-wrapped below
        # Let auth failures surface their own actionable message.
        if type(exc).__name__ == "TokenInvalidError":
            raise
        raise MediaNormalizationError(url, exc) from exc

    if body is None or not str(body).strip():
        raise MediaNormalizationError(url)

    return str(body).strip()


def normalize_media_urls(urls: list, client, hosts: set[str] | None = None) -> list:
    """Normalize every media URL in the list.

    Empty/None input is returned as-is (no endpoint calls). The order of the
    list is preserved.
    """
    if not urls:
        return urls
    hosts = hosts if hosts is not None else normalized_hosts()
    return [normalize_url(u, client, hosts) for u in urls]
