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

Beyond the Java behaviour, this module adds two robustness features driven by
production failures (see the journalctl analysis, Jun 2026):
  - **Local/sandbox paths are rejected up-front** with an actionable message.
    When ChatGPT fails to rewrite an attached/generated file it sends the raw
    sandbox path (e.g. ``/mnt/data/foo.png``) as a string. The backend can never
    fetch it, so we short-circuit with a recoverable hint instead of the
    misleading "make the link publicly accessible".
  - **One retry on transient backend failures** (timeouts / HTTP 5xx).
  - **Category-aware error messages** (Drive vs ephemeral AI/CDN links vs
    generic) so the model can self-correct.

This module only depends on the stdlib + a duck-typed client exposing
``normalize_image_url(url) -> str`` so it can be unit-tested in isolation.
"""

import logging
import os
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Hosts whose assets Metricool can already download directly — never renormalized.
DEFAULT_NORMALIZED_HOSTS = (
    "static.metricool.com",
    "metricool-download.s3.eu-west-1.amazonaws.com",
    "metricool-data.s3.eu-west-1.amazonaws.com",
)

# A media item that matches this is a path on the caller's machine / the model's
# sandbox (e.g. ChatGPT's ``/mnt/data``, a Windows ``C:\`` path, ``file://``,
# ``./relative`` or ``~/``). Metricool's backend can never download it, so it is
# rejected before any network call. Note ``/(?!/)`` matches an absolute POSIX
# path but NOT a protocol-relative ``//host/path`` URL.
_LOCAL_PATH_RE = re.compile(
    r"""^\s*(
          /(?!/)              # /mnt/data, /Users/..., /tmp/...
        | [A-Za-z]:[\\/]      # C:\... or C:/...
        | \\\\                 # \\server\share (UNC)
        | \.{1,2}[\\/]        # ./foo or ../foo
        | ~[\\/]              # ~/foo
        | file://             # file:// URI
    )""",
    re.VERBOSE,
)

# Hosts/links that hand out short-lived, signed URLs (AI image generators, CDN
# artifacts, tunnels). When normalization fails for these the most likely cause
# is expiry, so we tailor the hint.
_EPHEMERAL_MARKERS = (
    "oaiusercontent.com",   # OpenAI / ChatGPT generated files (SAS expires fast)
    "cloudfront.net",       # signed CloudFront artifacts (_jwt expires)
    "canva.com",            # Canva share/export links
    "canva.link",
    ".loca.lt",             # localtunnel dev tunnels (often already closed)
    "blob.core.windows.net",
)


def normalized_hosts() -> set[str]:
    """Trusted hosts, overridable via METRICOOL_NORMALIZED_HOSTS (CSV)."""
    raw = os.environ.get("METRICOOL_NORMALIZED_HOSTS")
    if raw:
        return {h.strip() for h in raw.split(",") if h.strip()}
    return set(DEFAULT_NORMALIZED_HOSTS)


def _is_local_path(url: str) -> bool:
    """True if the item is a local/sandbox file path, not a fetchable URL."""
    return bool(_LOCAL_PATH_RE.match(url))


def _user_message(url: str) -> str:
    """Build the user-facing failure message, tailored to the URL category.

    The message is what the MCP client (ChatGPT / Claude) sees, so it is phrased
    to give the model a concrete recovery action.
    """
    lowered = url.lower()

    if _is_local_path(url):
        return (
            f"The media at '{url}' is a file on the local machine (or the model's "
            "sandbox), not a public link, so Metricool can't download it. If you "
            "attached or generated this file, re-attach it to the message so it "
            "uploads as a file (it should arrive with a 'download_url'), or provide "
            "a public https:// URL instead."
        )

    if "drive.google.com" in lowered or "docs.google.com" in lowered:
        return (
            f"Couldn't upload the media at '{url}' to Metricool. Make sure the link "
            "is publicly accessible. For Google Drive links, ensure the user has "
            "linked Google Drive in their Metricool account."
        )

    if any(marker in lowered for marker in _EPHEMERAL_MARKERS):
        return (
            f"Couldn't upload the media at '{url}' to Metricool — the link may have "
            "expired. Temporary AI-generated or CDN links often stop working within "
            "minutes. Regenerate the file and attach it, or use a stable public "
            "https:// URL."
        )

    return (
        f"Couldn't upload the media at '{url}' to Metricool. "
        "Make sure the link is publicly accessible. "
        "For Google Drive links, ensure the user has linked Google Drive in "
        "their Metricool account."
    )


class MediaNormalizationError(Exception):
    """A media URL could not be normalized by the Metricool backend.

    The message is the user-facing text surfaced to the MCP client, tailored to
    the URL category (local path / Drive / ephemeral link / generic).
    """

    def __init__(self, url: str, cause: Exception | None = None) -> None:
        self.url = url
        super().__init__(_user_message(url))
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


def _is_transient(exc: Exception) -> bool:
    """True if a normalization failure is worth retrying once.

    Duck-typed so this module stays stdlib-only: timeouts/connection resets by
    class name, and HTTP errors carrying a 5xx response.
    """
    if type(exc).__name__ in {
        "ReadTimeout",
        "ConnectTimeout",
        "Timeout",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "ConnectionError",
        "TimeoutError",
    }:
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and status >= 500


def normalize_url(url: str, client, hosts: set[str]) -> str:
    """Normalize a single media URL, returning the (possibly new) URL.

    Trusted host -> returned unchanged. A local/sandbox path -> rejected up-front
    (the backend can't fetch it). Otherwise the normalization endpoint is called
    (with one retry on transient failures) and its trimmed text body replaces the
    URL. Auth errors (TokenInvalidError) propagate untouched; any other failure
    or an empty/blank body raises MediaNormalizationError.
    """
    # Non-string entries can't be normalized — leave them as-is.
    if not isinstance(url, str):
        return url

    if is_already_normalized(url, hosts):
        return url

    # A local/sandbox path will never be fetchable — fail fast with a clear,
    # recoverable message and skip the wasted backend round-trip.
    if _is_local_path(url):
        logger.info("normalize_url rejecting local/sandbox path: %s", url)
        raise MediaNormalizationError(url)

    attempts = 2  # initial try + one retry on transient failures
    for attempt in range(attempts):
        try:
            body = client.normalize_image_url(url)
            break
        except Exception as exc:  # noqa: BLE001 — re-wrapped below
            # Let auth failures surface their own actionable message.
            if type(exc).__name__ == "TokenInvalidError":
                raise
            if attempt + 1 < attempts and _is_transient(exc):
                logger.warning(
                    "normalize_url transient failure (%s), retrying: %s",
                    type(exc).__name__, url,
                )
                continue
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
