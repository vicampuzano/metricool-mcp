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
from collections import defaultdict
from datetime import datetime

import requests
from fields_loader import field_labels
from oauth import is_jwt

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("METRICOOL_BASE_URL", "https://app.metricool.com").rstrip("/")

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
        self._session = requests.Session()
        if is_jwt(token):
            self._session.headers.update({"Authorization": f"Bearer {token}"})
        else:
            self._session.headers.update({"X-Mc-Auth": token})

    def _get(self, path: str, params: dict | None = None) -> object:
        params = params or {}
        params.setdefault("integrationSource", "MCP")
        url = f"{_BASE_URL}{path}"
        logger.debug("GET %s params=%s", url, params)
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, params: dict | None = None, body: dict | None = None) -> object:
        params = params or {}
        params.setdefault("integrationSource", "MCP")
        url = f"{_BASE_URL}{path}"
        logger.debug("POST %s params=%s", url, params)
        resp = self._session.post(url, params=params, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, params: dict | None = None, body: dict | None = None) -> object:
        params = params or {}
        params.setdefault("integrationSource", "MCP")
        url = f"{_BASE_URL}{path}"
        logger.debug("PUT %s params=%s", url, params)
        resp = self._session.put(url, params=params, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------------------------
    # Settings
    # -------------------------------------------------------------------------

    def get_brands(self) -> object:
        return self._get("/api/v2/settings/brands")

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

    def create_scheduled_post(self, blog_id: str, date: str, post_info: dict) -> object:
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

        all_results: list = []
        api_params_base = {
            "start": from_dt.strftime(_ANALYTICS_DATE_FMT),
            "end": to_dt.strftime(_ANALYTICS_DATE_FMT),
            "blogId": brand_id,
            "userToken": self._token,
        }
        labels = field_labels()

        # EV fields: single call, add evdate dimension
        if ev_fields:
            ordered = ev_fields + ["evdate"]
            fields_str = ",".join(ordered)
            rows = self._get(
                "/api/datastudio/datasets",
                params={**api_params_base, "fields": fields_str},
            )
            if rows:
                all_results.append({
                    "group": "evolution",
                    "data": _rows_to_objects(rows, ordered, labels),
                })

        # Non-EV fields: one call per 4-char group
        for group_key, ids in non_ev_grouped.items():
            fields_str = ",".join(ids)
            rows = self._get(
                "/api/datastudio/datasets",
                params={**api_params_base, "fields": fields_str},
            )
            if rows:
                all_results.append({
                    "group": group_key,
                    "data": _rows_to_objects(rows, ids, labels),
                })

        return all_results


def _rows_to_objects(
    rows: list,
    field_ids: list[str],
    labels: dict[str, str],
) -> list[dict]:
    """Convert positional arrays from the API into named objects.

    Input:  [[24562, 27, 1, "20260409"], ...]
    Output: [{"Followers": 24562, "Follows": 27, "Posts": 1, "date": "2026-04-09"}, ...]
    """
    keys = [labels.get(fid, fid) for fid in field_ids]
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

    # Instagram: rename collaborator field 'username' → 'name' (API expects 'name')
    if "instagramData" in post_info and post_info["instagramData"] is not None:
        ig = dict(post_info["instagramData"])
        if "collaborators" in ig and ig["collaborators"]:
            ig["collaborators"] = [
                {"name": c.get("username", c.get("name", "")), "deleted": c.get("deleted", False)}
                for c in ig["collaborators"]
                if c
            ]
        body["instagramData"] = ig

    return body
