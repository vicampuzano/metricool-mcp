"""
Metricool API HTTP client.

Equivalent to the Java RestTemplate repositories + interceptors:
  - AuthorizationInterceptor  → X-Mc-Auth or Authorization: Bearer header on every request
  - IntegrationSourceInterceptor → integrationSource=MCP on every request
  - RestTemplateBrandRepository
  - RestTemplateSchedulerRepository
  - RestTemplateAnalyticsRepository

Auth header selection (mirrors Java AuthorizationInterceptor):
  - JWT token (3 dot-separated parts) → Authorization: Bearer <jwt>
  - Plain API key                     → X-Mc-Auth: <key>
"""

import logging
import os
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from fields_loader import field_labels, field_raw_labels
from oauth import is_jwt

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("METRICOOL_BASE_URL", "https://app.metricool.com").rstrip("/")


def base_url() -> str:
    """The Metricool app base URL. Also used to build planner deep links."""
    return _BASE_URL


# Split connect/read timeouts, mirroring the Java RestTemplate settings
# (metricool.api.connect-timeout / read-timeout). A dead backend now fails in
# seconds instead of hanging on the single 30s budget the whole call used to share.
_CONNECT_TIMEOUT = float(os.environ.get("METRICOOL_CONNECT_TIMEOUT", "5"))
_READ_TIMEOUT = float(os.environ.get("METRICOOL_READ_TIMEOUT", "20"))
_TIMEOUT = (_CONNECT_TIMEOUT, _READ_TIMEOUT)

# Upper bound on concurrent analytics queries when a call splits into several
# incompatible field groups.
_MAX_PARALLEL_QUERIES = 6


class TokenInvalidError(Exception):
    """The Metricool API rejected the bearer token (401/403).

    Raised when the token is revoked or otherwise invalid before its 'exp'
    claim. Surfaced to the caller so the LLM/user gets an actionable message
    instead of a generic HTTP error.
    """

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        message = (
            "Metricool authentication is no longer valid "
            "(API returned HTTP %d). Please reconnect the Metricool MCP."
            % status_code
        )
        if detail:
            message += f" Details: {detail}"
        super().__init__(message)

# Date format used by the scheduler API  (yyyy-MM-dd'T'HH:mm:ss)
_SCHEDULER_DATE_FMT = "%Y-%m-%dT%H:%M:%S"
# Date format used by the analytics API (yyyyMMdd)
_ANALYTICS_DATE_FMT = "%Y%m%d"


def _parse_date(value: str) -> datetime:
    """Parse a date/datetime string in common formats.

    Accepted inputs (most specific first):
      - 2025-03-15T14:30:00+02:00  (ISO 8601 with offset)
      - 2025-03-15T14:30:00        (ISO 8601 without offset)
      - 2025-03-15T14:30+02:00
      - 2025-03-15T14:30
      - 2025-03-15 14:30:00        (space separator)
      - 2025-03-15 14:30
      - 2025-03-15                  (date only → midnight)
    """
    clean = value.strip().split(".")[0]  # drop fractional seconds
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M%z",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {value!r}")


class MetricoolClient:
    """
    Thin wrapper around requests that adds auth + integrationSource to every call.

    Auth header mirrors the Java AuthorizationInterceptor logic:
      - JWT token  → Authorization: Bearer <jwt>
      - API key    → X-Mc-Auth: <key>
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._headers = (
            {"Authorization": f"Bearer {token}"}
            if is_jwt(token)
            else {"X-Mc-Auth": token}
        )
        # requests.Session is not thread-safe, and media normalization fans a
        # single call out across a thread pool, so each thread gets its own.
        self._local = threading.local()

    @property
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
    ) -> object:
        params = params or {}
        params.setdefault("integrationSource", "MCP")
        url = f"{_BASE_URL}{path}"
        logger.debug("%s %s params=%s", method, url, params)
        resp = self._session.request(
            method, url, params=params, json=body, timeout=_TIMEOUT
        )
        if resp.status_code in (401, 403):
            raise TokenInvalidError(resp.status_code, resp.text[:200])
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> object:
        return self._request("GET", path, params=params)

    def _post(self, path: str, params: dict | None = None, body: dict | None = None) -> object:
        return self._request("POST", path, params=params, body=body)

    def _put(self, path: str, params: dict | None = None, body: dict | None = None) -> object:
        return self._request("PUT", path, params=params, body=body)

    # -------------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------------

    def get_brands(self) -> object:
        return self._get("/api/v2/settings/brands")

    # -------------------------------------------------------------------------
    # Media
    # -------------------------------------------------------------------------

    def normalize_image_url(self, url: str) -> str:
        """Normalize a media URL via the Metricool backend.

        The backend downloads the asset and returns a new URL hosted on
        Metricool infrastructure. Unlike the other endpoints, the response body
        is plain text (the new URL), so this does not go through ``_request``
        (which expects JSON). Auth failures still surface as TokenInvalidError.
        """
        url_ = f"{_BASE_URL}/api/actions/normalize/image/url"
        params = {"url": url, "folder": "PLANNER", "integrationSource": "MCP"}
        logger.debug("GET %s url=%s", url_, url)
        resp = self._session.get(url_, params=params, timeout=_TIMEOUT)
        if resp.status_code in (401, 403):
            raise TokenInvalidError(resp.status_code, resp.text[:200])
        resp.raise_for_status()
        return resp.text.strip()

    # -------------------------------------------------------------------------
    # Scheduler
    # -------------------------------------------------------------------------

    def get_scheduled_posts(
        self,
        brand_id: str,
        from_date: str,
        to_date: str,
        timezone: str,
        extended_range: bool = False,
    ) -> object:
        from_dt = _parse_date(from_date)
        to_dt = _parse_date(to_date)
        return self._get(
            "/api/v2/scheduler/posts",
            params={
                "blogId": brand_id,
                "start": from_dt.strftime(_SCHEDULER_DATE_FMT),
                "end": to_dt.strftime(_SCHEDULER_DATE_FMT),
                "timezone": timezone,
                "extendedRange": str(extended_range).lower(),
            },
        )

    def get_best_time(
        self,
        brand_id: str,
        from_date: str,
        to_date: str,
        timezone: str,
        social_network: str,
    ) -> object:
        from_dt = _parse_date(from_date)
        to_dt = _parse_date(to_date)
        return self._get(
            f"/api/v2/scheduler/besttimes/{social_network}",
            params={
                "blogId": brand_id,
                "start": from_dt.strftime(_SCHEDULER_DATE_FMT),
                "end": to_dt.strftime(_SCHEDULER_DATE_FMT),
                "timezone": timezone,
            },
        )

    def get_pinterest_boards(self, brand_id: str) -> list[dict]:
        """List the Pinterest boards connected to a brand.

        Used to resolve a board name to the numeric board id the API requires.
        Returns a list of ``{"id": ..., "name": ...}`` dicts (empty on an
        unexpected response shape).
        """
        result = self._get(
            "/api/v2/scheduler/boards/pinterest", params={"brandId": brand_id}
        )
        data = result.get("data", result) if isinstance(result, dict) else result
        if not isinstance(data, list):
            return []
        return [b for b in data if isinstance(b, dict)]

    def create_scheduled_post(self, blog_id: str, post_info: dict) -> object:
        body = _build_post_request(post_info)
        return self._post(
            "/api/v2/scheduler/posts",
            params={"blogId": blog_id},
            body=body,
        )

    def update_scheduled_post(
        self, post_id: str, uuid: str, blog_id: str, post_info: dict
    ) -> object:
        body = _build_post_request(post_info, post_id=post_id, uuid=uuid)
        return self._put(
            f"/api/v2/scheduler/posts/{post_id}",
            params={"blogId": blog_id},
            body=body,
        )

    # -------------------------------------------------------------------------
    # Analytics
    # -------------------------------------------------------------------------

    def get_analytics_data(
        self,
        brand_id: str,
        field_ids: list[str],
        from_date: str,
        to_date: str,
    ) -> list:
        from_dt = _parse_date(from_date)
        to_dt = _parse_date(to_date)

        # ------------------------------------------------------------------
        # Group field IDs into compatible batches.
        #
        # Field ID structure: 2-char network + 2-char connector + 2-digit index
        #   e.g. FBPO01 = Facebook (FB) + Posts (PO) + metric 01
        #
        # Rules:
        #   - EV (evolution) fields can be combined across networks in a
        #     single call.  They always require the "evdate" dimension.
        #   - All other fields must share the same 4-char prefix
        #     (network + connector) to be combined in one call.
        # ------------------------------------------------------------------
        ev_fields: list[str] = []
        non_ev_grouped: dict[str, list[str]] = defaultdict(list)

        for fid in field_ids:
            if len(fid) >= 4 and fid[2:4].upper() == "EV":
                ev_fields.append(fid)
            elif len(fid) >= 4:
                group_key = fid[:4].upper()
                non_ev_grouped[group_key].append(fid)

        api_params_base = {
            "start": from_dt.strftime(_ANALYTICS_DATE_FMT),
            "end": to_dt.strftime(_ANALYTICS_DATE_FMT),
            "blogId": brand_id,
            "userToken": self._token,
        }

        # Build the query list: EV fields go in one call (with the evdate
        # dimension appended), each non-EV group gets its own.
        queries: list[tuple[str, list[str]]] = []
        if ev_fields:
            queries.append(("evolution", ev_fields + ["evdate"]))
        queries.extend(non_ev_grouped.items())

        if not queries:
            return []

        def run(query: tuple[str, list[str]]) -> dict | None:
            group_key, ids = query
            rows = self._get(
                "/api/datastudio/datasets",
                params={**api_params_base, "fields": ",".join(ids)},
            )
            if not rows:
                return None
            return {"group": group_key, "data": _rows_to_objects(rows, ids)}

        if len(queries) == 1:
            results = [run(queries[0])]
        else:
            # Independent backend calls — run them concurrently so the total wait
            # is the slowest query, not their sum (Java ES5MPTM3-5955). Group
            # order is preserved; a failure in any query propagates.
            with ThreadPoolExecutor(
                max_workers=min(len(queries), _MAX_PARALLEL_QUERIES),
                thread_name_prefix="analytics",
            ) as pool:
                results = list(pool.map(run, queries))

        return [r for r in results if r]


def _resolve_output_keys(field_ids: list[str]) -> list[str]:
    """Pick display keys for a row, disambiguating label collisions.

    Stripped labels are compact ("Followers") but collide when combining metrics
    from different networks (IG + TH + TT all yield "Followers"). When a
    collision is detected, every field sharing that label falls back to its
    raw prefixed label ("Instagram Evolution > Followers"). Non-colliding
    fields keep their short label.
    """
    short = field_labels()
    keys = [short.get(fid, fid) for fid in field_ids]
    counts: dict[str, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    collisions = {k for k, c in counts.items() if c > 1}
    if not collisions:
        return keys
    raw = field_raw_labels()
    return [
        raw.get(fid, keys[i]) if keys[i] in collisions else keys[i]
        for i, fid in enumerate(field_ids)
    ]


def _rows_to_objects(rows: list, field_ids: list[str]) -> list[dict]:
    """Convert positional arrays from the API into named objects.

    Input:  [[24562, 27, 1, "20260409"], ...]
    Output: [{"Followers": 24562, "Follows": 27, "Posts": 1, "date": "2026-04-09"}, ...]
    """
    keys = _resolve_output_keys(field_ids)
    result = []
    for row in rows:
        if not isinstance(row, list):
            continue
        obj = {}
        for i, key in enumerate(keys):
            val = row[i] if i < len(row) else None
            if val is None:
                continue  # skip nulls to keep response lean
            # Format YYYYMMDD dates as YYYY-MM-DD for readability
            if key == "date" and isinstance(val, str) and len(val) == 8:
                val = f"{val[:4]}-{val[4:6]}-{val[6:]}"
            obj[key] = val
        result.append(obj)
    return result


# ---------------------------------------------------------------------------
# Post request body builder
# Mirrors the Java ScheduledPostRequestDto.from() logic.
# ---------------------------------------------------------------------------

def _build_post_request(
    post_info: dict,
    post_id: str | None = None,
    uuid: str | None = None,
) -> dict:
    """
    Convert the post_info dict (as received from the LLM tool parameter)
    into the JSON body that the Metricool scheduler API expects.

    Most fields are passed through directly. The only transformations are:
      - id / uuid are injected for updates
      - Instagram collaborators: rename 'username' → 'name' (API field name)
    """
    body: dict = {}

    if post_id is not None:
        body["id"] = post_id
    if uuid is not None:
        body["uuid"] = uuid

    body["autoPublish"] = post_info.get("autoPublish", True)
    body["descendants"] = post_info.get("descendants", [])
    body["draft"] = post_info.get("draft", False)
    body["firstCommentText"] = post_info.get("firstCommentText", "")
    body["hasNotReadNotes"] = post_info.get("hasNotReadNotes", False)
    body["media"] = post_info.get("media", [])
    body["mediaAltText"] = post_info.get("mediaAltText", [])
    body["providers"] = post_info.get("providers", [])
    body["publicationDate"] = post_info.get("publicationDate", {})
    body["shortener"] = post_info.get("shortener", False)
    body["smartLinkData"] = post_info.get("smartLinkData", {"ids": []})
    body["text"] = post_info.get("text", "")

    # Network-specific data — pass through with minor fixes
    for key in (
        "twitterData",
        "facebookData",
        "linkedinData",
        "pinterestData",
        "youtubeData",
        "twitchData",
        "tiktokData",
        "blueskyData",
        "threadsData",
    ):
        if key in post_info and post_info[key] is not None:
            body[key] = post_info[key]

    # Instagram: collaborators go under 'username'. The backend used to read
    # 'name', which is why this used to rename the field; it now expects
    # 'username' (Java ES5MPTM3-6752), so accept either spelling from the LLM
    # and always emit 'username'.
    if "instagramData" in post_info and post_info["instagramData"] is not None:
        ig = dict(post_info["instagramData"])
        if "collaborators" in ig and ig["collaborators"]:
            ig["collaborators"] = [
                {
                    "username": c.get("username", c.get("name", "")),
                    "deleted": c.get("deleted", False),
                }
                for c in ig["collaborators"]
                if c
            ]
        body["instagramData"] = ig

    # Ensure minimal network-specific data for each provider when the LLM
    # omits it.  Only networks whose defaults are safe (no required user input).
    _safe_defaults = {
        "twitter": ("twitterData", {"tags": []}),
        "facebook": ("facebookData", {"type": "POST"}),
        "instagram": ("instagramData", {"type": "POST", "showReelOnFeed": True}),
        "linkedin": ("linkedinData", {"type": "post", "previewIncluded": True}),
        "bluesky": ("blueskyData", {"postLanguages": []}),
        "threads": ("threadsData", {"allowedCountryCodes": []}),
        "twitch": ("twitchData", {"autoPublish": True, "tags": []}),
    }
    for provider in body.get("providers", []):
        network = (provider.get("network") or "").lower()
        if network in _safe_defaults:
            key, default = _safe_defaults[network]
            if key not in body:
                body[key] = dict(default)

    logger.debug("_build_post_request body: %s", body)
    return body
