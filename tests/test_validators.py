"""Validation rules, mirroring the Java PostInfoValidator behaviour."""

import pytest

from validators import validate_post_info


def _post(networks, text="hello", **extra):
    info = {
        "providers": [{"network": n} for n in networks],
        "text": text,
        "media": [],
        "autoPublish": True,
    }
    info.update(extra)
    return info


# --- X/Twitter: no local character limit (depends on the brand's subscription) ---


def test_long_twitter_text_is_allowed():
    """X Premium accounts publish long-form posts; the backend is the authority."""
    validate_post_info(_post(["twitter"], text="x" * 5000))


def test_bluesky_limit_still_enforced():
    with pytest.raises(ValueError, match="300-character limit"):
        validate_post_info(_post(["bluesky"], text="x" * 301))


def test_bluesky_at_limit_is_allowed():
    validate_post_info(_post(["bluesky"], text="x" * 300))


# --- Story text rules ---


def test_auto_story_alone_rejects_text():
    with pytest.raises(ValueError, match="Instagram Story published automatically"):
        validate_post_info(
            _post(["instagram"], text="caption", media=["a.jpg"],
                  instagramData={"type": "STORY"})
        )


def test_facebook_auto_story_alone_rejects_text():
    with pytest.raises(ValueError, match="Facebook Story published automatically"):
        validate_post_info(
            _post(["facebook"], text="caption", media=["a.jpg"],
                  facebookData={"type": "STORY"})
        )


def test_story_with_another_network_allows_text():
    """The text belongs to the other network; the Story just ignores it."""
    validate_post_info(
        _post(["instagram", "twitter"], text="caption", media=["a.jpg"],
              instagramData={"type": "STORY"})
    )


def test_manual_story_allows_text():
    """In manual mode the user pastes the text themselves."""
    validate_post_info(
        _post(["instagram"], text="caption", media=["a.jpg"],
              autoPublish=False, instagramData={"type": "STORY"})
    )


def test_story_without_text_is_valid():
    validate_post_info(
        _post(["instagram"], text="", media=["a.jpg"],
              instagramData={"type": "STORY"})
    )


def test_text_required_for_non_story():
    with pytest.raises(ValueError, match="'text' is required"):
        validate_post_info(
            _post(["instagram"], text="", media=["a.jpg"],
                  instagramData={"type": "POST"})
        )


# --- TRIAL_REEL ---


@pytest.mark.parametrize("ig_type", ["REEL", "TRIAL_REEL"])
def test_reel_types_require_media(ig_type):
    """TRIAL_REEL carries the same video requirement as REEL."""
    with pytest.raises(ValueError, match="Instagram Reel requires a video"):
        validate_post_info(
            _post(["instagram"], media=[], instagramData={"type": ig_type})
        )


@pytest.mark.parametrize("ig_type", ["REEL", "TRIAL_REEL"])
def test_reel_types_accept_a_video(ig_type):
    validate_post_info(
        _post(["instagram"], media=["a.mp4"], instagramData={"type": ig_type})
    )
