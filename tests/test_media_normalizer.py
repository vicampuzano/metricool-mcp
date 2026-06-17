"""Tests for media_normalizer (Feature 1)."""

import pytest

from media_normalizer import (
    DEFAULT_NORMALIZED_HOSTS,
    MediaNormalizationError,
    normalize_media_urls,
    normalized_hosts,
)

TRUSTED = set(DEFAULT_NORMALIZED_HOSTS)


class FakeClient:
    """Records normalize_image_url calls and returns a canned/computed body."""

    def __init__(self, response="https://static.metricool.com/normalized.jpg", raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def normalize_image_url(self, url):
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.response(url) if callable(self.response) else self.response


def test_trusted_host_not_called_and_unchanged():
    client = FakeClient()
    url = "https://static.metricool.com/already.jpg"
    out = normalize_media_urls([url], client, TRUSTED)
    assert out == [url]
    assert client.calls == []  # endpoint never hit


def test_external_url_is_normalized_and_replaced():
    client = FakeClient(response="https://static.metricool.com/new.jpg")
    url = "https://drive.google.com/file/d/abc/view"
    out = normalize_media_urls([url], client, TRUSTED)
    assert out == ["https://static.metricool.com/new.jpg"]
    assert client.calls == [url]


def test_body_is_trimmed():
    client = FakeClient(response="  https://static.metricool.com/x.jpg  \n")
    out = normalize_media_urls(["https://drive.google.com/x"], client, TRUSTED)
    assert out == ["https://static.metricool.com/x.jpg"]


@pytest.mark.parametrize("body", ["", "   ", "\n\t", None])
def test_blank_or_null_body_raises(body):
    client = FakeClient(response=body)
    with pytest.raises(MediaNormalizationError) as exc:
        normalize_media_urls(["https://drive.google.com/x"], client, TRUSTED)
    assert "drive.google.com" in str(exc.value)
    assert "Google Drive" in str(exc.value)


def test_http_error_raises_media_error():
    client = FakeClient(raises=RuntimeError("boom"))
    with pytest.raises(MediaNormalizationError) as exc:
        normalize_media_urls(["https://drive.google.com/x"], client, TRUSTED)
    assert exc.value.url == "https://drive.google.com/x"
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_token_invalid_error_propagates():
    class TokenInvalidError(Exception):
        pass

    client = FakeClient(raises=TokenInvalidError("401"))
    with pytest.raises(TokenInvalidError):
        normalize_media_urls(["https://drive.google.com/x"], client, TRUSTED)


def test_empty_media_no_calls():
    client = FakeClient()
    assert normalize_media_urls([], client, TRUSTED) == []
    assert normalize_media_urls(None, client, TRUSTED) is None
    assert client.calls == []


def test_invalid_url_is_attempted_not_crashed():
    client = FakeClient(response="https://static.metricool.com/ok.jpg")
    out = normalize_media_urls(["not a url"], client, TRUSTED)
    # host can't be extracted -> treated as not normalized -> endpoint called
    assert client.calls == ["not a url"]
    assert out == ["https://static.metricool.com/ok.jpg"]


def test_mixed_list_preserves_order_and_only_normalizes_external():
    client = FakeClient(response=lambda u: "https://static.metricool.com/dl.jpg")
    urls = [
        "https://static.metricool.com/keep.jpg",       # trusted
        "https://drive.google.com/external",            # external
        "https://metricool-data.s3.eu-west-1.amazonaws.com/keep2.jpg",  # trusted
    ]
    out = normalize_media_urls(urls, client, TRUSTED)
    assert out == [
        "https://static.metricool.com/keep.jpg",
        "https://static.metricool.com/dl.jpg",
        "https://metricool-data.s3.eu-west-1.amazonaws.com/keep2.jpg",
    ]
    assert client.calls == ["https://drive.google.com/external"]


def test_normalized_hosts_env_override(monkeypatch):
    monkeypatch.setenv("METRICOOL_NORMALIZED_HOSTS", "a.example.com, b.example.com")
    assert normalized_hosts() == {"a.example.com", "b.example.com"}
    monkeypatch.delenv("METRICOOL_NORMALIZED_HOSTS")
    assert normalized_hosts() == set(DEFAULT_NORMALIZED_HOSTS)


# ---------------------------------------------------------------------------
# Local/sandbox path rejection (ChatGPT /mnt/data, Windows paths, etc.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/mnt/data/ghostwriter_images/generated/poster_1.png",
        "/mnt/data/June 23 Stories Frame 1.png",
        "/Users/someone/Pictures/photo.png",
        "/tmp/output.jpg",
        r"C:\Users\tdmv\Documents\image.png",
        "C:/Users/tdmv/image.png",
        r"\\server\share\image.png",
        "./relative/image.png",
        "../up/image.png",
        "~/Pictures/image.png",
        "file:///mnt/data/x.png",
    ],
)
def test_local_path_rejected_without_backend_call(path):
    client = FakeClient()
    with pytest.raises(MediaNormalizationError) as exc:
        normalize_media_urls([path], client, TRUSTED)
    assert client.calls == []  # short-circuited, no wasted round-trip
    assert exc.value.url == path
    msg = str(exc.value)
    assert "local machine" in msg
    assert "download_url" in msg  # tells the model how to recover


def test_protocol_relative_url_is_not_treated_as_local():
    # // is a protocol-relative URL, not an absolute path -> still hits backend.
    client = FakeClient(response="https://static.metricool.com/ok.jpg")
    out = normalize_media_urls(["//cdn.example.com/x.jpg"], client, TRUSTED)
    assert client.calls == ["//cdn.example.com/x.jpg"]
    assert out == ["https://static.metricool.com/ok.jpg"]


# ---------------------------------------------------------------------------
# Category-aware messages
# ---------------------------------------------------------------------------


def test_ephemeral_link_message_mentions_expiry():
    url = "https://sdmntprnorthcentralus.oaiusercontent.com/files/abc/raw?se=2026"
    client = FakeClient(response="")  # blank body -> failure
    with pytest.raises(MediaNormalizationError) as exc:
        normalize_media_urls([url], client, TRUSTED)
    assert "expired" in str(exc.value).lower()


def test_drive_message_unchanged():
    url = "https://drive.google.com/file/d/abc/view"
    client = FakeClient(response="")
    with pytest.raises(MediaNormalizationError) as exc:
        normalize_media_urls([url], client, TRUSTED)
    assert "Google Drive" in str(exc.value)


# ---------------------------------------------------------------------------
# Retry on transient backend failures
# ---------------------------------------------------------------------------


class SequenceClient:
    """Raises/returns a scripted sequence across successive calls."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def normalize_image_url(self, url):
        self.calls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ReadTimeout(Exception):
    """Mirrors requests.exceptions.ReadTimeout (matched by class name)."""


class _FakeHTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


def test_transient_timeout_is_retried_then_succeeds():
    client = SequenceClient([ReadTimeout("timed out"),
                             "https://static.metricool.com/ok.jpg"])
    out = normalize_media_urls(["https://x.example.com/a.jpg"], client, TRUSTED)
    assert out == ["https://static.metricool.com/ok.jpg"]
    assert len(client.calls) == 2  # retried once


def test_transient_5xx_is_retried():
    client = SequenceClient([_FakeHTTPError(503),
                             "https://static.metricool.com/ok.jpg"])
    out = normalize_media_urls(["https://x.example.com/a.jpg"], client, TRUSTED)
    assert out == ["https://static.metricool.com/ok.jpg"]
    assert len(client.calls) == 2


def test_transient_failure_twice_gives_up():
    client = SequenceClient([ReadTimeout("t1"), ReadTimeout("t2")])
    with pytest.raises(MediaNormalizationError):
        normalize_media_urls(["https://x.example.com/a.jpg"], client, TRUSTED)
    assert len(client.calls) == 2  # initial + one retry, no more


def test_non_transient_error_is_not_retried():
    client = SequenceClient([RuntimeError("boom"),
                             "https://static.metricool.com/ok.jpg"])
    with pytest.raises(MediaNormalizationError):
        normalize_media_urls(["https://x.example.com/a.jpg"], client, TRUSTED)
    assert len(client.calls) == 1  # 4xx/other -> no retry


def test_4xx_http_error_is_not_retried():
    client = SequenceClient([_FakeHTTPError(400),
                             "https://static.metricool.com/ok.jpg"])
    with pytest.raises(MediaNormalizationError):
        normalize_media_urls(["https://x.example.com/a.jpg"], client, TRUSTED)
    assert len(client.calls) == 1
