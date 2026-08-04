"""Coverage for the changes ported from the Java MCP server.

Groups: planner URLs, OAuth discovery metadata and route variants, /MCP path
normalization, the Instagram collaborator field name, and the custom-field merge.
"""

import pytest

pytest.importorskip("mcp")

import client as client_mod  # noqa: E402
import fields_loader  # noqa: E402
import server  # noqa: E402
from middleware import BearerAuthMiddleware  # noqa: E402
from oauth import OAUTH_ROUTES, oauth_authorization_server_metadata  # noqa: E402


# --- plannerUrl (Java ES5MPTM3-5685) ---


def test_planner_url_added_to_single_post():
    result = server._add_planner_urls({"data": {"uuid": "abc-123"}}, "31927")
    assert result["data"]["plannerUrl"] == (
        "https://app.metricool.com/planner/calendar"
        "?blogId=31927&openWithPostUuid=abc-123"
    )


def test_planner_url_added_to_every_post_in_a_list():
    result = server._add_planner_urls(
        {"data": [{"uuid": "a"}, {"uuid": "b"}]}, "1"
    )
    assert all("plannerUrl" in p for p in result["data"])


def test_planner_url_skipped_without_uuid():
    result = server._add_planner_urls({"data": {"id": "7"}}, "1")
    assert "plannerUrl" not in result["data"]


def test_planner_url_tolerates_unexpected_shapes():
    assert server._add_planner_urls([], "1") == []
    assert server._add_planner_urls({"data": "oops"}, "1") == {"data": "oops"}


def test_create_returns_planner_url(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def create_scheduled_post(self, blog_id, post_info):
            return {"data": {"uuid": "u-1"}}

    monkeypatch.setattr(server, "get_api_key", lambda: "tok")
    monkeypatch.setattr(server, "MetricoolClient", FakeClient)
    monkeypatch.setattr(server, "normalize_media_urls", lambda urls, c, hosts=None: urls)
    monkeypatch.setattr(server, "validate_post_info", lambda pi: None)

    result = server.create_scheduled_post(
        blog_id="31927",
        date="2099-01-01T10:00:00",
        timezone="Europe/Madrid",
        networks=["twitter"],
        text="hi",
    )
    assert "openWithPostUuid=u-1" in result["data"]["plannerUrl"]


# --- OAuth metadata + discovery routes (Java ES5MPTM3-6464 / ES5MPTM3-5817) ---


def test_client_secret_post_is_advertised():
    import asyncio
    import json

    response = asyncio.run(oauth_authorization_server_metadata(None))
    body = json.loads(response.body)
    assert body["token_endpoint_auth_methods_supported"] == [
        "client_secret_basic",
        "client_secret_post",
        "none",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
        "/.well-known/oauth-authorization-server/mcp",
        "/.well-known/openid-configuration/mcp",
        "/mcp/.well-known/oauth-authorization-server",
        "/mcp/.well-known/openid-configuration",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/mcp/.well-known/oauth-protected-resource",
    ],
)
def test_discovery_route_is_registered(path):
    assert path in {r.path for r in OAUTH_ROUTES}


# --- auth exemptions and /MCP normalization (Java ES5MPTM3-6473) ---


def _run_middleware(path):
    """Drive the middleware for one request, returning (seen_scope, status)."""
    import asyncio

    seen = {}

    async def downstream(scope, receive, send):
        seen["scope"] = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    app = BearerAuthMiddleware(downstream)
    asyncio.run(
        app({"type": "http", "path": path, "method": "GET", "headers": []}, receive, send)
    )
    return seen.get("scope"), sent[0]["status"]


def test_mcp_well_known_suffix_is_public():
    scope, status = _run_middleware("/mcp/.well-known/openid-configuration")
    assert status == 200 and scope is not None


def test_root_well_known_is_public():
    _, status = _run_middleware("/.well-known/openid-configuration")
    assert status == 200


def test_uppercase_mcp_path_is_normalized():
    """/MCP must behave like /mcp — here that means the same 401, not a 404."""
    scope, status = _run_middleware("/MCP")
    assert status == 401  # still requires auth
    assert scope is None


def test_uppercase_mcp_well_known_is_normalized_and_public():
    scope, status = _run_middleware("/MCP/.well-known/openid-configuration")
    assert status == 200
    assert scope["path"] == "/mcp/.well-known/openid-configuration"


def test_mcp_endpoint_still_requires_auth():
    _, status = _run_middleware("/mcp")
    assert status == 401


# --- Instagram collaborators (Java ES5MPTM3-6752) ---


def _build(post_info):
    return client_mod._build_post_request(post_info)


def test_collaborators_are_sent_under_username():
    body = _build({
        "instagramData": {"collaborators": [{"username": "someone"}]},
        "providers": [{"network": "instagram"}],
    })
    assert body["instagramData"]["collaborators"] == [
        {"username": "someone", "deleted": False}
    ]


def test_legacy_name_key_is_accepted_and_renamed():
    body = _build({
        "instagramData": {"collaborators": [{"name": "legacy", "deleted": True}]},
        "providers": [{"network": "instagram"}],
    })
    assert body["instagramData"]["collaborators"] == [
        {"username": "legacy", "deleted": True}
    ]


# --- custom field merge (Java DataStudioFieldDictionaryLoader) ---


def test_custom_fields_replace_generated_ones():
    fields = {f["fieldId"]: f for f in fields_loader.load_fields()}
    # IGEV9999 supersedes IGEV08, which must no longer be exposed.
    assert "IGEV08" not in fields
    assert fields["IGEV9999"]["dataAggregation"] == "AVG"


def test_new_java_fields_are_present():
    ids = {f["fieldId"] for f in fields_loader.load_fields()}
    for new_field in ("IGAC01", "IGPF11", "LIPF01", "THCO01", "TTTW29", "IGPO29"):
        assert new_field in ids


def test_no_duplicate_field_ids():
    ids = [f["fieldId"] for f in fields_loader.load_fields()]
    assert len(ids) == len(set(ids))


# --- analytics fan-out (Java ES5MPTM3-5955) ---


def _client_with_stubbed_get(responses, delay=0.0):
    """A MetricoolClient whose _get returns canned rows per 'fields' param."""
    import time

    c = client_mod.MetricoolClient("plain-api-key")
    seen = []

    def fake_get(path, params=None):
        seen.append(params["fields"])
        if delay:
            time.sleep(delay)
        return responses.get(params["fields"], [])

    c._get = fake_get
    return c, seen


def test_analytics_splits_incompatible_groups():
    c, seen = _client_with_stubbed_get({
        "IGEV01,evdate": [[1, "20260101"]],
        "FBPO01": [[2]],
        "IGPO01": [[3]],
    })
    out = c.get_analytics_data("1", ["IGEV01", "FBPO01", "IGPO01"], "2026-01-01", "2026-01-31")
    groups = {r["group"] for r in out}
    assert groups == {"evolution", "FBPO", "IGPO"}
    assert len(seen) == 3


def test_analytics_groups_run_concurrently():
    import time

    c, _ = _client_with_stubbed_get(
        {"FBPO01": [[1]], "IGPO01": [[2]], "TWTW01": [[3]], "LIPO01": [[4]]},
        delay=0.05,
    )
    started = time.monotonic()
    out = c.get_analytics_data(
        "1", ["FBPO01", "IGPO01", "TWTW01", "LIPO01"], "2026-01-01", "2026-01-31"
    )
    elapsed = time.monotonic() - started
    assert len(out) == 4
    assert elapsed < 0.15, f"sequential fallback? took {elapsed:.2f}s for 4x50ms"


def test_analytics_drops_empty_groups():
    c, _ = _client_with_stubbed_get({"FBPO01": [[1]], "IGPO01": []})
    out = c.get_analytics_data("1", ["FBPO01", "IGPO01"], "2026-01-01", "2026-01-31")
    assert [r["group"] for r in out] == ["FBPO"]


def test_analytics_without_fields_makes_no_call():
    c, seen = _client_with_stubbed_get({})
    assert c.get_analytics_data("1", [], "2026-01-01", "2026-01-31") == []
    assert seen == []
