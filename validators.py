"""
Post validation rules.

Mirrors the Java PostInfoValidator + network-specific ValidationRule classes.
Raises ValueError with a user-friendly message on failure.
"""


def validate_post_info(post_info: dict) -> None:
    """Run all validation rules against post_info dict. Raises ValueError on failure."""
    providers = [p.get("network", "").lower() for p in post_info.get("providers", []) if p]
    if not providers:
        raise ValueError("At least one provider is required in 'providers'.")

    text = post_info.get("text", "")
    ig_data = post_info.get("instagramData", {}) or {}
    ig_type = (ig_data.get("type") or "POST").upper()

    # Instagram Story has no text requirement
    if "instagram" in providers and ig_type == "STORY":
        if text:
            raise ValueError("Instagram Story cannot have 'text'.")
    else:
        if not isinstance(text, str) or text.strip() == "":
            raise ValueError("Field 'text' is required.")

    if "twitter" in providers and len(text) > 280:
        raise ValueError(
            "Error: The text exceeds the 280-character limit allowed on X. Please edit it."
        )

    if "bluesky" in providers and len(text) > 300:
        raise ValueError(
            "Error: The text exceeds the 300-character limit allowed on Bluesky. Please edit it."
        )

    media = post_info.get("media", []) or []

    if "instagram" in providers:
        _validate_instagram(ig_type, media)

    if "pinterest" in providers:
        _validate_pinterest(post_info.get("pinterestData") or {}, media)

    if "youtube" in providers:
        _validate_youtube(post_info.get("youtubeData") or {})

    if "tiktok" in providers:
        _validate_tiktok(post_info.get("tiktokData") or {}, media)

    if "facebook" in providers:
        fb_data = post_info.get("facebookData", {}) or {}
        fb_type = (fb_data.get("type") or "POST").upper()
        _validate_facebook(fb_type, media)


def _validate_instagram(ig_type: str, media: list) -> None:
    if ig_type == "REEL":
        if not _has_video(media):
            raise ValueError("Instagram Reel requires a video.")
    elif ig_type == "STORY":
        if not media:
            raise ValueError("Instagram Story requires at least one image or video.")
    else:  # POST
        if not media:
            raise ValueError("Instagram Post requires at least one image or video.")


def _validate_pinterest(pinterest_data: dict, media: list) -> None:
    if not media:
        raise ValueError("Pinterest post requires at least one image.")
    if not pinterest_data.get("boardId"):
        raise ValueError("Pinterest post requires 'boardId'.")
    if not pinterest_data.get("pinTitle"):
        raise ValueError("Pinterest post requires 'pinTitle'.")
    if not pinterest_data.get("pinLink"):
        raise ValueError("Pinterest post requires 'pinLink'.")


def _validate_youtube(youtube_data: dict) -> None:
    if not youtube_data.get("title"):
        raise ValueError("YouTube post requires 'title' in youtubeData.")
    if "madeForKids" not in youtube_data:
        raise ValueError("YouTube post requires 'madeForKids' in youtubeData.")


def _validate_tiktok(tiktok_data: dict, media: list) -> None:
    if not media:
        raise ValueError("TikTok post requires at least one image or video.")
    if not tiktok_data.get("title"):
        raise ValueError("TikTok post requires 'title' in tiktokData.")


def _validate_facebook(fb_type: str, media: list) -> None:
    if fb_type == "REEL":
        if not _has_video(media):
            raise ValueError("Facebook Reel requires a video.")
    elif fb_type == "STORY":
        if not media:
            raise ValueError("Facebook Story requires at least one image or video.")


def _has_video(media: list) -> bool:
    """Heuristic: an item is a video if its URL contains common video extensions."""
    video_extensions = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")
    for item in media:
        url = item if isinstance(item, str) else ""
        if any(url.lower().endswith(ext) for ext in video_extensions):
            return True
    return bool(media)  # If we can't tell, assume at least one is a video
