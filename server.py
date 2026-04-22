"""
Metricool MCP Server — Python port of the Java Spring AI implementation.

Transport : Streamable HTTP (ASGI / uvicorn)
Endpoint  : POST /mcp
Auth      : Authorization: Bearer <metricool-api-key>
            or MCP_API_KEY env var (single-user / local mode)
"""

import json
import logging
import os
import re
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from auth import get_api_key
from client import MetricoolClient
from fields_loader import available_connectors_for_network, filter_fields, load_fields
from validators import validate_post_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="Metricool",
    instructions=(
        "You are a Metricool AI assistant that helps users manage social media "
        "scheduling, analytics, and account settings.\n\n"
        "WORKFLOW — follow this order:\n"
        "1. Always call get_brand_settings first to obtain the brand_id and timezone. "
        "Do NOT guess or ask the user for brand_id — get it from this call.\n"
        "2. For analytics:\n"
        "   a. Call get_analytics_available_metrics with the right network and connector "
        "to discover valid field IDs.\n"
        "   b. Select only the fields relevant to the user's question (see fieldType).\n"
        "   c. Call get_analytics_data_by_metrics with those field IDs.\n"
        "3. For scheduling: use get_scheduled_posts to list, create_scheduled_post or "
        "update_scheduled_post to modify. Call get_best_time_to_post_by_network if the "
        "user doesn't specify a time.\n\n"
        "ANALYTICS STRATEGY:\n"
        "- For account trends and summaries (followers, reach over time) → connector=evolution.\n"
        "- For individual content performance (best posts, top reels) → connector=posts, reels, "
        "stories, or videos depending on the content type.\n"
        "- Request only the metrics the user needs. Skip heavy dimensions (text, image, URL) "
        "unless the user wants to see specific post content.\n"
        "- If the request is ambiguous, confirm with the user before fetching data.\n\n"
        "GENERAL RULES:\n"
        "- Do NOT retry failed calls — report the error to the user.\n"
        "- Do NOT modify user text on error — notify them and let them fix it.\n"
        "- Ask for clarification when needed before performing any action."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["mcp.metricool.ai", "127.0.0.1:*", "localhost:*"],
        allowed_origins=[
            "https://mcp.metricool.ai",
            "https://claude.ai",
            "https://claude.com",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    ),
)

# ---------------------------------------------------------------------------
# Settings tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Get the list of brands from your Metricool account. "
        "Only Instagram, Facebook, Twitch, YouTube, Twitter, and Bluesky support competitors."
    ),
    annotations=ToolAnnotations(
        title="Get Brand Settings",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def get_brand_settings() -> dict:
    logger.info("get_brand_settings called")
    result = MetricoolClient(get_api_key()).get_brands()
    # Trim response to only what the LLM needs — saves ~60% tokens
    brands = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(brands, list):
        trimmed = [_trim_brand(b) for b in brands]
        logger.info("get_brand_settings returning %d brands", len(trimmed))
        return trimmed
    return result


def _trim_brand(b: dict) -> dict:
    """Keep only the fields the LLM needs from a brand object."""
    trimmed = {
        "id": b.get("id"),
        "label": b.get("label"),
        "timezone": b.get("timezone"),
        "connectedNetworks": _clean_network_names(b.get("networksData") or {}),
    }
    # Include role and permissions for shared brands so the LLM knows
    # what actions are allowed (e.g. can it schedule posts or only view analytics)
    role = b.get("brandRole")
    if role:
        actions = role.get("actions", {})
        trimmed["role"] = role.get("name")
        trimmed["canSchedule"] = actions.get("schedulePosts", False)
        trimmed["canViewAnalytics"] = actions.get("viewAnalytics", False)
    return trimmed


# Map raw API keys from networksData to the network names used in analytics tools
_NETWORK_KEY_MAP = {
    "facebookData": "facebook",
    "instagramData": "instagram",
    "twitterData": "twitter",
    "linkedinData": "linkedin",
    "youtubeData": "youtube",
    "tiktokData": "tiktok",
    "twitchData": "twitch",
    "pinterestData": "pinterest",
    "threadsData": "threads",
    "blueskyData": "bluesky",
    "webData": "website",
    "gbpData": "googleBusinessProfile",
    "facebookAdsData": "facebookAds",
    "googleAdsData": "googleAds",
    "tiktokAdsData": "tiktokAds",
}


def _clean_network_names(networks_data: dict) -> list[str]:
    """Convert raw API keys to the network names accepted by analytics tools."""
    return [_NETWORK_KEY_MAP.get(k, k) for k in networks_data]


# ---------------------------------------------------------------------------
# Scheduler tools
# ---------------------------------------------------------------------------


def _parse_info(info: str | dict) -> dict:
    """Accept post info as dict (natural for LLMs) or JSON string (legacy)."""
    if isinstance(info, dict):
        return info
    try:
        return json.loads(info)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid JSON in 'info': {exc}") from exc


_DT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2})(?::(\d{2}))?)?")


def _normalise_datetime(value: str) -> str:
    """Return YYYY-MM-DDTHH:mm:ss, stripping any tz offset or fractional seconds."""
    m = _DT_RE.match(value.strip())
    if not m:
        return value
    date, hhmm, ss = m.group(1), m.group(2) or "00:00", m.group(3) or "00"
    return f"{date}T{hhmm}:{ss}"


def _ensure_publication_date(post_info: dict, date: str, timezone: str) -> None:
    """Normalise publicationDate to {dateTime, timezone}, filling gaps from params.

    Handles every format LLMs have been observed to send:
      - missing / None / {}       → built entirely from date + timezone params
      - plain string              → treated as dateTime, timezone from param
      - dict with partial fields  → gaps filled from params
      - any other type            → replaced with params
    """
    pub = post_info.get("publicationDate")
    if isinstance(pub, str):
        pub = {"dateTime": pub.strip()} if pub.strip() else {}
    elif not isinstance(pub, dict):
        pub = {}
    if not pub.get("dateTime"):
        pub["dateTime"] = date
    if not pub.get("timezone"):
        pub["timezone"] = timezone
    # API expects YYYY-MM-DDTHH:mm:ss — strip offset / fractional seconds
    pub["dateTime"] = _normalise_datetime(pub["dateTime"])
    post_info["publicationDate"] = pub


@mcp.tool(
    description=(
        "Get posts from the Metricool planner for a specific brand. "
        "Returns all posts in the date range: already published, pending, and drafts. "
        "Use this to check what is scheduled for a given week, review upcoming content, "
        "or see what was published from Metricool in a past period. "
        "Keep date ranges short (max 1 month). For content performance analytics use "
        "get_analytics_data_by_metrics instead."
    ),
    annotations=ToolAnnotations(
        title="Get Scheduled Posts",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def get_scheduled_posts(
    brand_id: str,
    from_date: str,
    to_date: str,
    timezone: str,
    extended_range: bool = False,
) -> dict:
    """
    Args:
        brand_id: Blog id of the Metricool brand account.
        from_date: Start date and time. Use format YYYY-MM-DDTHH:mm:ss (example: 2025-03-15T00:00:00). Do NOT include timezone offset in the value.
        to_date: End date and time. Use format YYYY-MM-DDTHH:mm:ss (example: 2025-03-15T23:59:59). Do NOT include timezone offset in the value.
        timezone: IANA timezone identifier (example: Europe/Madrid). Use the value from get_brand_settings.
        extended_range: When true, search range is expanded one day in each direction. Default false.
    """
    logger.info(
        "get_scheduled_posts called: brand_id=%s from=%s to=%s tz=%s extended=%s",
        brand_id, from_date, to_date, timezone, extended_range,
    )
    result = MetricoolClient(get_api_key()).get_scheduled_posts(
        brand_id, from_date, to_date, timezone, extended_range
    )
    posts = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(posts, list):
        trimmed = [_trim_post(p) for p in posts]
        logger.info("get_scheduled_posts returning %d posts", len(trimmed))
        return trimmed
    return result


def _trim_post(p: dict) -> dict:
    """Keep only the fields the LLM needs from a planner post."""
    pub = p.get("publicationDate", {})
    providers = p.get("providers", [])
    trimmed = {
        "id": p.get("id"),
        "uuid": p.get("uuid"),
        "date": pub.get("dateTime"),
        "timezone": pub.get("timezone"),
        "text": p.get("text", ""),
        "networks": [
            {"network": pr.get("network"), "status": pr.get("detailedStatus", pr.get("status"))}
            for pr in providers if pr
        ],
        "draft": p.get("draft", False),
    }
    media = p.get("media")
    if media:
        trimmed["mediaCount"] = len(media)
    return trimmed


@mcp.tool(
    description=(
        "Get the best time to post for a specific provider. "
        "Returns a list of hours and days with a score — the higher the value, the better the time. "
        "Try to cover at most 1 week. "
        "If you have a day but no hours, use the start and end of that day."
    ),
    annotations=ToolAnnotations(
        title="Get Best Time to Post",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def get_best_time_to_post_by_network(
    brand_id: str,
    from_date: str,
    to_date: str,
    timezone: str,
    social_network: str,
) -> dict:
    """
    Args:
        brand_id: Blog id of the Metricool brand account.
        from_date: Start date and time. Use format YYYY-MM-DDTHH:mm:ss (example: 2025-03-15T00:00:00). Do NOT include timezone offset in the value.
        to_date: End date and time. Use format YYYY-MM-DDTHH:mm:ss (example: 2025-03-15T23:59:59). Do NOT include timezone offset in the value.
        timezone: IANA timezone identifier (example: Europe/Madrid). Use the value from get_brand_settings.
        social_network: Accepted values: "twitter", "facebook", "instagram", "linkedin", "youtube", "tiktok".
    """
    logger.info(
        "get_best_time_to_post_by_network called: brand_id=%s network=%s",
        brand_id, social_network,
    )
    return MetricoolClient(get_api_key()).get_best_time(
        brand_id, from_date, to_date, timezone, social_network
    )


_NETWORK_ALIASES = {"x": "twitter", "ig": "instagram", "fb": "facebook",
                     "yt": "youtube", "tt": "tiktok", "li": "linkedin", "pin": "pinterest"}


def _resolve_networks(networks: list[str]) -> list[str]:
    """Lower-case and resolve aliases like 'x' → 'twitter'."""
    return [_NETWORK_ALIASES.get(n.lower(), n.lower()) for n in networks]


def _build_post_info(
    networks: list[str], text: str, date: str, timezone: str,
    media: list | None, draft: bool, content_type: str, first_comment: str,
    pinterest_board_id: str, pinterest_pin_title: str, pinterest_pin_link: str,
    youtube_title: str, youtube_made_for_kids: bool, tiktok_title: str,
) -> dict:
    """Assemble the full post_info dict from flat tool parameters."""
    info: dict = {
        "providers": [{"network": n} for n in networks],
        "publicationDate": {"dateTime": _normalise_datetime(date), "timezone": timezone},
        "text": text,
        "media": media or [],
        "mediaAltText": [],
        "autoPublish": True,
        "descendants": [],
        "draft": draft,
        "firstCommentText": first_comment,
        "hasNotReadNotes": False,
        "shortener": False,
        "smartLinkData": {"ids": []},
    }
    ct = (content_type or "POST").upper()
    if "twitter" in networks:
        info["twitterData"] = {"tags": []}
    if "instagram" in networks:
        info["instagramData"] = {"type": ct, "showReelOnFeed": True}
    if "facebook" in networks:
        info["facebookData"] = {"type": ct}
    if "linkedin" in networks:
        info["linkedinData"] = {"type": "post", "previewIncluded": True}
    if "pinterest" in networks:
        info["pinterestData"] = {"boardId": pinterest_board_id, "pinTitle": pinterest_pin_title,
                                 "pinLink": pinterest_pin_link, "pinNewFormat": False}
    if "youtube" in networks:
        info["youtubeData"] = {"title": youtube_title, "type": "video", "privacy": "public",
                               "tags": [], "madeForKids": youtube_made_for_kids}
    if "tiktok" in networks:
        info["tiktokData"] = {"title": tiktok_title, "privacyOption": "PUBLIC_TO_EVERYONE",
                              "disableComment": False, "disableDuet": False, "disableStitch": False,
                              "autoAddMusic": False, "photoCoverIndex": 0}
    if "bluesky" in networks:
        info["blueskyData"] = {"postLanguages": []}
    if "threads" in networks:
        info["threadsData"] = {"allowedCountryCodes": []}
    if "twitch" in networks:
        info["twitchData"] = {"autoPublish": True, "tags": []}
    return info


@mcp.tool(
    description="""Schedule a post to one or more social networks via Metricool.
Call get_brand_settings first to obtain blog_id and timezone.
Use get_best_time_to_post_by_network if the user doesn't specify a time.

Simple example — Twitter/X text post:
  blog_id="31927", networks=["twitter"], text="Hello world!", date="2026-04-13T17:00:00", timezone="Europe/Madrid"

Character limits (do NOT split into threads or truncate — report the error):
- X/Twitter: 280 chars max.
- Bluesky: 300 chars max.

Media requirements:
- Instagram POST/REEL/STORY: requires media. REEL needs video. STORY has no text.
- Pinterest: requires media + pinterest_board_id, pinterest_pin_title, pinterest_pin_link.
- YouTube: requires media (video) + youtube_title.
- TikTok: requires media + tiktok_title.
- Facebook REEL: requires video. Facebook STORY: requires media.

content_type applies to Instagram and Facebook only: POST (default), REEL, or STORY.
The date must be in the future. DO NOT modify the user's text — just report any error.""",
    annotations=ToolAnnotations(
        title="Create Scheduled Post",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def create_scheduled_post(
    blog_id: str,
    date: str,
    timezone: str,
    networks: list[str],
    text: str = "",
    media: list[str] | None = None,
    draft: bool = False,
    content_type: str = "POST",
    first_comment: str = "",
    pinterest_board_id: str = "",
    pinterest_pin_title: str = "",
    pinterest_pin_link: str = "",
    youtube_title: str = "",
    youtube_made_for_kids: bool = False,
    tiktok_title: str = "",
) -> dict:
    """
    Args:
        blog_id: Blog id of the Metricool brand account (from get_brand_settings).
        date: Publication date/time, format YYYY-MM-DDTHH:mm:ss (e.g. 2025-03-15T14:30:00).
        timezone: IANA timezone (e.g. "Europe/Madrid"). Use the value from get_brand_settings.
        networks: Social networks to publish to (e.g. ["twitter"] or ["twitter","instagram"]). Accepted: twitter, instagram, facebook, linkedin, bluesky, threads, pinterest, youtube, tiktok, twitch.
        text: Post text content. Required for all networks except Instagram Story.
        media: List of public media URLs (images/videos). Required for Instagram, Pinterest, YouTube, TikTok.
        draft: Save as draft instead of scheduling (default false).
        content_type: POST, REEL, or STORY — only used for Instagram and Facebook (default POST).
        first_comment: Optional first comment to add after publishing.
        pinterest_board_id: Pinterest board ID (required when pinterest in networks).
        pinterest_pin_title: Pin title (required when pinterest in networks).
        pinterest_pin_link: Destination URL for the pin (required when pinterest in networks).
        youtube_title: Video title (required when youtube in networks).
        youtube_made_for_kids: Whether the video is made for kids (required when youtube in networks).
        tiktok_title: Video title (required when tiktok in networks).
    """
    nets = _resolve_networks(networks)
    logger.info("create_scheduled_post called: date=%s blog_id=%s tz=%s nets=%s", date, blog_id, timezone, nets)
    post_info = _build_post_info(
        nets, text, date, timezone, media, draft, content_type, first_comment,
        pinterest_board_id, pinterest_pin_title, pinterest_pin_link,
        youtube_title, youtube_made_for_kids, tiktok_title,
    )
    validate_post_info(post_info)
    result = MetricoolClient(get_api_key()).create_scheduled_post(blog_id, post_info)
    logger.info("create_scheduled_post result: %s", result)
    return result


@mcp.tool(
    description="""Update a scheduled post in Metricool.
Get the post id and uuid from get_scheduled_posts first.
Ask the user to confirm what will be changed before proceeding.
Include the full original content in the request, modifying only the fields that change.
The same media requirements as create_scheduled_post apply.
The date cannot be in the past. Do not retry on error.""",
    annotations=ToolAnnotations(
        title="Update Scheduled Post",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def update_scheduled_post(
    id: str,
    uuid: str,
    blog_id: str,
    info: str | dict,
) -> dict:
    """
    Args:
        id: Post id from get_scheduled_posts.
        uuid: Post uuid from get_scheduled_posts.
        blog_id: Blog id of the Metricool brand account.
        info: Post data as a JSON object (same format as create_scheduled_post).
    """
    logger.info("update_scheduled_post called: id=%s uuid=%s blog_id=%s", id, uuid, blog_id)
    post_info = _parse_info(info)
    validate_post_info(post_info)

    result = MetricoolClient(get_api_key()).update_scheduled_post(id, uuid, blog_id, post_info)
    logger.info("update_scheduled_post result: %s", result)
    return result


# ---------------------------------------------------------------------------
# Analytics tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Retrieves the list of available metrics for a specific social network and connector. "
        "Only active (non-deprecated) metrics are returned. "
        "Always call this BEFORE get_analytics_data_by_metrics to discover valid field IDs.\n\n"
        "IMPORTANT: Always provide BOTH network AND connector to keep the response small. "
        "If you need multiple connectors (e.g. evolution + posts), make separate calls. "
        "Never call without a connector — the full list is too large and wastes tokens.\n\n"
        "How to choose the right connector:\n"
        "- 'evolution': Account-level daily aggregates over time (followers, reach, impressions, "
        "engagement). Best for timelines, trends, and account performance summaries.\n"
        "- 'posts', 'reels', 'stories', 'videos': Individual content performance (per-post "
        "metrics like likes, comments, reach). Best for analyzing specific publications. "
        "Each content type is a separate connector — e.g. for Instagram use 'posts' for feed "
        "posts, 'reels' for Reels, 'stories' for Stories.\n"
        "- 'competitors': Competitor account-level data.\n"
        "- 'competitor posts', 'competitor reels', 'competitor videos': Competitor content-level data.\n"
        "- 'campaigns', 'ads': Ad platform campaign/ad-level data (Meta Ads, Google Ads, TikTok Ads).\n"
        "- 'country', 'age and gender', 'demographic': Audience demographics.\n"
        "- 'hashtags': Hashtag performance (Instagram).\n"
        "- 'keywords': Search keyword data (Google Business Profile, Google Ads).\n"
        "- 'traffic source', 'sources', 'pages': Website traffic breakdowns.\n\n"
        "Examples:\n"
        "- 'How did my Instagram grow this month?' → network=instagram, connector=evolution\n"
        "- 'Which of my Instagram posts performed best?' → network=instagram, connector=posts\n"
        "- 'Show me my Instagram Reels performance' → network=instagram, connector=reels\n"
        "- 'Compare followers across all my networks' → connector=evolution (no network filter)\n\n"
        "Each returned field has a fieldType ('dimension' or 'metric'):\n"
        "- Dimensions: descriptive attributes (date, post text, image URL, post URL, type). "
        "Include only the dimensions the user actually needs.\n"
        "- Metrics: numeric values that can be aggregated (likes, reach, engagement, comments).\n\n"
        "Each metric also carries an 'aggregation' hint telling you how to combine it over time:\n"
        "- SUM: value is additive across days (e.g. Posts Published, Likes received). Safe to sum over a range.\n"
        "- AVG: daily average already computed (e.g. Daily engagement timeline). Do NOT sum — average if aggregating further.\n"
        "- LAST: point-in-time snapshot — use the last value in the range (e.g. Followers count). Summing is WRONG.\n"
        "- SINGLE: pre-aggregated value for the whole period (e.g. Aggregated Engagement). Use as-is, one number per range.\n"
        "- (absent): the field is a dimension, not a metric.\n"
        "Pay attention to this when answering 'totals' — asking for total followers means taking LAST, not SUM.\n\n"
        "Ambiguous connector codes to disambiguate (case-insensitive):\n"
        "- linkedin: 'DF' = demographics by function, 'DI' = demographics by industry, 'DZ' = demographics by company size.\n"
        "- twitter: 'TW' = posts (Twitter uses 'TW' instead of the usual 'posts').\n"
        "- twitch: 'CL' = clips.\n"
        "- network 'SL' (Smart Links): 'BT' = buttons, 'IM' = images, 'TL' = timeline table.\n"
        "- network 'FG' (Facebook Groups): use 'evolution' or 'posts'.\n\n"
        "When the user asks for aggregated stats or rankings, request only the relevant metrics "
        "plus minimal dimensions (e.g. date). Do NOT request text content, images, or URLs "
        "unless the user wants to see individual post details.\n"
        "If the user's request is ambiguous, confirm which metrics matter before fetching data.\n\n"
        "Response shape: normally a list of field objects. If the filter matches 0 active fields "
        "(e.g. a deprecated connector), the response is an object with 'fields': [], a 'hint' "
        "explaining the situation, and 'availableConnectors' listing valid connectors for the network."
    ),
    annotations=ToolAnnotations(
        title="Get Available Analytics Metrics",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_analytics_available_metrics(
    network: Optional[str] = None,
    connector: Optional[str] = None,
) -> list | dict:
    """
    Args:
        network: Network name. Values: bluesky, googleBusinessProfile, metaAds, threads,
            facebookAds, facebook, instagram, linkedin, pinterest, tiktok, tiktokAds,
            twitter, twitch, youtube, brandInfo, website, brandSummary.
        connector: Connector type. Determines the level of data granularity.
            Account-level over time: evolution.
            Content-level (per post/video): posts, reels, stories, videos, photos, posts & photos.
            Competitors: competitors, competitor posts, competitor reels, competitor videos.
            Ads: campaigns, ads, promoted reels, promoted posts.
            Demographics: age and gender, gender, country, demographic, reach distribution.
            Website: traffic source, sources, pages, keywords.
            Other: hashtags, info.
            Do not combine connectors in one call. For TikTok videos use "posts".
    """
    logger.info("get_analytics_available_metrics called: network=%s connector=%s", network, connector)
    fields = load_fields()
    result = filter_fields(fields, network, connector)
    if not result and network:
        suggestions = available_connectors_for_network(network)
        logger.info(
            "get_analytics_available_metrics returned 0 fields for network=%s connector=%s; suggesting %d connectors",
            network, connector, len(suggestions),
        )
        return {
            "fields": [],
            "hint": (
                f"No active fields for network={network}"
                + (f", connector={connector}" if connector else "")
                + ". Pick one of the connectors in 'availableConnectors' and call this tool again."
            ),
            "availableConnectors": suggestions,
        }
    logger.info("get_analytics_available_metrics returned %d fields", len(result))
    return result


@mcp.tool(
    description=(
        "Retrieves analytical data for a specified Metricool account over a given date range, "
        "based on a selected list of metrics. "
        "Returns data grouped by compatible metric sets.\n\n"
        "IMPORTANT — field ID compatibility rules:\n"
        "Each field ID has 6 characters: 2-char network + 2-char connector + 2-digit index "
        "(e.g. FBPO01 = Facebook Posts metric 01, IGRE03 = Instagram Reels metric 03).\n"
        "- Evolution fields (connector = EV, e.g. FBEV01, IGEV17, TWEV03) CAN be combined "
        "across different networks in one call. They are ideal for timelines.\n"
        "- All other fields MUST share the same 4-char prefix (network+connector) in one call. "
        "For example: FBPO01 + FBPO03 is valid, but FBPO01 + IGPO01 is NOT.\n"
        "- If you need metrics from different non-evolution groups (e.g. Facebook Posts AND "
        "Instagram Reels), make separate calls for each group.\n"
        "The server auto-splits incompatible fields into separate API calls and returns results "
        "grouped, but for clarity always prefer sending compatible fields together.\n\n"
        "EFFICIENCY — select only the fields you need:\n"
        "- For summaries or rankings, request only metric fields (likes, reach, engagement) "
        "plus a date dimension. Do NOT include text content, image URLs, or post URLs.\n"
        "- Include content dimensions (text, image, URL) only when the user wants to see "
        "or identify specific posts.\n"
        "- For evolution data, request only the specific metrics relevant to the user's question "
        "— not all available evolution metrics."
    ),
    annotations=ToolAnnotations(
        title="Get Analytics Data",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def get_analytics_data_by_metrics(
    brand_id: str,
    from_date: str,
    to_date: str,
    metrics: Optional[list] = None,
) -> list:
    """
    Args:
        brand_id: Brand id of the Metricool account.
        from_date: Start date. Use format YYYY-MM-DD (example: 2025-03-01). Only the date is needed, time is ignored.
        to_date: End date. Use format YYYY-MM-DD (example: 2025-03-31). Only the date is needed, time is ignored.
        metrics: List of Data Studio field IDs (format: 2-char network + 2-char connector + 2-digit index, e.g. IGPO01). Use get_analytics_available_metrics first to discover valid IDs.
    """
    logger.info(
        "get_analytics_data_by_metrics called: brand_id=%s metrics=%s", brand_id, metrics
    )
    result = MetricoolClient(get_api_key()).get_analytics_data(
        brand_id, metrics or [], from_date, to_date
    )
    logger.info("get_analytics_data_by_metrics returned %d rows", len(result))
    return result


# ---------------------------------------------------------------------------
# ASGI application (used by uvicorn)
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402

from middleware import BearerAuthMiddleware  # noqa: E402
from oauth import OAUTH_ROUTES  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402


async def _openai_challenge(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Vn5o8WqEp5PUp7gEgkOvSIFRtYzfK48hak_ih0SQiUo")


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

# Combine FastMCP's streamable-HTTP handler with the OAuth discovery routes
# into a single Starlette app.
#
# Route priority (first match wins):
#   /.well-known/*  → OAuth discovery endpoints (no auth required)
#   /*              → FastMCP handler (auth enforced by BearerAuthMiddleware)
_mcp_asgi = mcp.streamable_http_app()


@asynccontextmanager
async def _lifespan(app):
    """Propagate the MCP session manager lifecycle to the outer Starlette app."""
    async with mcp.session_manager.run():
        yield


_combined = Starlette(
    routes=[
        *OAUTH_ROUTES,
        Route("/health", _health),
        Route("/.well-known/openai-apps-challenge", _openai_challenge),
        Mount("/", app=_mcp_asgi),
    ],
    lifespan=_lifespan,
)

_combined = TrustedHostMiddleware(
    _combined,
    allowed_hosts=["mcp.metricool.ai", "*.metricool.ai", "ai.metricool.com", "localhost", "127.0.0.1"],
)

app = BearerAuthMiddleware(_combined)

# ---------------------------------------------------------------------------
# Entry point for direct execution  (python server.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
