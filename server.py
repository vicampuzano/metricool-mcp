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
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from auth import get_api_key
from client import MetricoolClient
from fields_loader import filter_fields, load_fields
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
    logger.info("get_brand_settings result: %s", result)
    # Trim response to only what the LLM needs — saves ~60% tokens
    brands = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(brands, list):
        return [
            {
                "id": b.get("id"),
                "label": b.get("label"),
                "timezone": b.get("timezone"),
                "connectedNetworks": list((b.get("networksData") or {}).keys()),
            }
            for b in brands
        ]
    return result


# ---------------------------------------------------------------------------
# Scheduler tools
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Get the list of scheduled posts for a specific Metricool brand (blog_id). "
        "Only retrieves posts that are scheduled (not yet published). "
        "If the user doesn't provide a blog_id, ask for it."
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
    logger.info("get_scheduled_posts result: %s", result)
    return result


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


@mcp.tool(
    description="""Schedule a post to Metricool at a specific date and time.
You can use get_best_time_to_post_by_network to determine the best time if the user doesn't specify one.
Requirements by network:
- Instagram: at least one image or video. Posts/carousels need an image, Reels need a video, Stories need image or video.
- Pinterest: image required + boardId in pinterestData.
- YouTube: video required + title + madeForKids in youtubeData.
- TikTok: at least one image or video + title in tiktokData.
- Facebook Reel: video required. Facebook Story: image or video required.
- Bluesky: text must not exceed 300 characters. If exceeded respond: "Error: The text exceeds the 300-character limit allowed on Bluesky. Please edit it."
- X (Twitter): text must not exceed 280 characters. Do NOT split into threads. If exceeded respond: "Error: The text exceeds the 280-character limit allowed on X. Please edit it."
The date cannot be in the past. DO NOT modify the text on error — just notify the user.""",
    annotations=ToolAnnotations(
        title="Create Scheduled Post",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def create_scheduled_post(
    date: str,
    blog_id: str,
    info: str,
) -> dict:
    """
    Args:
        date: Publication date/time. Use format YYYY-MM-DDTHH:mm:ss (example: 2025-03-15T14:30:00). Do NOT include timezone offset — timezone is set inside publicationDate in the info JSON.
        blog_id: Blog id of the Metricool brand account.
        info: JSON string with the post data. Required fields:
            providers (list, e.g. [{"network":"twitter"}]),
            publicationDate ({dateTime:"YYYY-MM-DDTHH:mm:ss", timezone:"IANA"}),
            text (str, required unless Instagram Story).
          Optional fields (defaults in parentheses):
            autoPublish (true), descendants ([]), draft (false),
            firstCommentText (""), hasNotReadNotes (false),
            media ([]), mediaAltText ([]), shortener (false),
            smartLinkData ({ids:[]}).
          Network data (include only for networks in providers):
            twitterData: {"tags":[]},
            facebookData: {"type":"POST|REEL|STORY","title":"<str>","boost":<float>,"boostPayer":"<str>","boostBeneficiary":"<str>"},
            instagramData: {"type":"POST|REEL|STORY","collaborators":[{"username":"<str>","deleted":false}],"showReelOnFeed":true,"boost":<float>,"boostPayer":"<str>","boostBeneficiary":"<str>"},
            linkedinData: {"documentTitle":"<str>","publishImagesAsPDF":false,"previewIncluded":true,"type":"post|poll","poll":{"question":"<str>","options":[{"text":"<str>"}],"settings":{"duration":"ONE_DAY|THREE_DAYS|SEVEN_DAYS|FOURTEEN_DAYS"}}},
            pinterestData: {"boardId":"<str>","pinTitle":"<str>","pinLink":"<str>","pinNewFormat":false},
            youtubeData: {"title":"<str>","type":"video|short","privacy":"public|unlisted|private","tags":[],"category":"<str>","madeForKids":false},
            twitchData: {"autoPublish":true,"tags":[]},
            tiktokData: {"disableComment":false,"disableDuet":false,"disableStitch":false,"privacyOption":"PUBLIC_TO_EVERYONE|MUTUAL_FOLLOW_FRIENDS|FOLLOWER_OF_CREATOR|SELF_ONLY","commercialContentThirdParty":false,"commercialContentOwnBrand":false,"title":"<str>","autoAddMusic":false,"photoCoverIndex":0},
            blueskyData: {"postLanguages":[]},
            threadsData: {"allowedCountryCodes":[]}.
    """
    logger.info("create_scheduled_post called: date=%s blog_id=%s", date, blog_id)
    try:
        post_info = json.loads(info)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in 'info': {exc}") from exc

    validate_post_info(post_info)

    result = MetricoolClient(get_api_key()).create_scheduled_post(blog_id, date, post_info)
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
    info: str,
) -> dict:
    """
    Args:
        id: Post id from get_scheduled_posts.
        uuid: Post uuid from get_scheduled_posts.
        blog_id: Blog id of the Metricool brand account.
        info: JSON string with the full post data (same format as create_scheduled_post).
    """
    logger.info("update_scheduled_post called: id=%s uuid=%s blog_id=%s", id, uuid, blog_id)
    try:
        post_info = json.loads(info)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in 'info': {exc}") from exc

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
        "- Metrics: numeric values that can be aggregated (likes, reach, engagement, comments).\n"
        "When the user asks for aggregated stats or rankings, request only the relevant metrics "
        "plus minimal dimensions (e.g. date). Do NOT request text content, images, or URLs "
        "unless the user wants to see individual post details.\n"
        "If the user's request is ambiguous, confirm which metrics matter before fetching data."
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
) -> list:
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
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402


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
