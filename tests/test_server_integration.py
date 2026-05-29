"""Integration tests that need the full MCP stack (run on the server venv).

Skipped automatically where `mcp` isn't installed (e.g. the local test venv).
"""

import asyncio

import pytest

pytest.importorskip("mcp")

import server  # noqa: E402


def _get_tool(name):
    tools = asyncio.run(server.mcp.list_tools())
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name} not found")


def test_create_tool_exposes_mediafile_and_meta():
    tool = _get_tool("create_scheduled_post")
    props = tool.inputSchema.get("properties", {})
    assert "mediaFile" in props, "mediaFile must be in the input schema"
    # Must be declared as a single file object, NOT an array — OpenAI's
    # proxied-mount handling rejects array/nested file params.
    assert "array" not in str(props["mediaFile"]), "mediaFile must not be an array"
    assert tool.meta == {"openai/fileParams": ["mediaFile"]}


def test_other_tools_have_no_filepicker_meta():
    tool = _get_tool("get_scheduled_posts")
    assert not (tool.meta or {}).get("openai/fileParams")


def test_normalize_runs_before_validate(monkeypatch):
    """The order must be: normalize media -> validate -> send."""
    order = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def create_scheduled_post(self, blog_id, post_info):
            order.append("send")
            return {"ok": True, "media": post_info["media"]}

    def fake_normalize(urls, client, hosts=None):
        order.append("normalize")
        return ["https://static.metricool.com/normalized.jpg" for _ in urls]

    def fake_validate(post_info):
        order.append("validate")
        # validation sees the already-normalized URLs
        assert post_info["media"] == ["https://static.metricool.com/normalized.jpg"]

    monkeypatch.setattr(server, "get_api_key", lambda: "tok")
    monkeypatch.setattr(server, "MetricoolClient", FakeClient)
    monkeypatch.setattr(server, "normalize_media_urls", fake_normalize)
    monkeypatch.setattr(server, "validate_post_info", fake_validate)

    result = server.create_scheduled_post(
        blog_id="1",
        date="2099-01-01T10:00:00",
        timezone="Europe/Madrid",
        networks=["instagram"],
        text="hi",
        media=["https://drive.google.com/x"],
    )
    assert order == ["normalize", "validate", "send"]
    assert result["media"] == ["https://static.metricool.com/normalized.jpg"]


def test_chatgpt_mediafile_merged_before_normalize(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def create_scheduled_post(self, blog_id, post_info):
            return {"ok": True}

    def fake_normalize(urls, client, hosts=None):
        seen["normalized_input"] = list(urls)
        return list(urls)

    monkeypatch.setattr(server, "get_api_key", lambda: "tok")
    monkeypatch.setattr(server, "MetricoolClient", FakeClient)
    monkeypatch.setattr(server, "normalize_media_urls", fake_normalize)
    monkeypatch.setattr(server, "validate_post_info", lambda pi: None)

    server.create_scheduled_post(
        blog_id="1",
        date="2099-01-01T10:00:00",
        timezone="Europe/Madrid",
        networks=["instagram"],
        text="hi",
        media=["https://existing/a.jpg"],
        mediaFile=server.MediaFile(download_url="https://chatgpt/b.jpg", file_id="f1"),
    )
    # ChatGPT download_url appended to existing media, then handed to normalize
    assert seen["normalized_input"] == ["https://existing/a.jpg", "https://chatgpt/b.jpg"]
