"""Tests for chatgpt_files (Feature 2 — merge helper)."""

from dataclasses import dataclass

from chatgpt_files import extract_download_urls


@dataclass
class FakeFile:
    download_url: str | None = None


def test_extract_from_dicts():
    files = [
        {"download_url": "https://x/1.jpg", "file_id": "a"},
        {"download_url": "https://x/2.jpg", "file_id": "b"},
    ]
    assert extract_download_urls(files) == ["https://x/1.jpg", "https://x/2.jpg"]


def test_extract_from_objects():
    files = [FakeFile("https://x/1.jpg"), FakeFile("https://x/2.jpg")]
    assert extract_download_urls(files) == ["https://x/1.jpg", "https://x/2.jpg"]


def test_skips_blank_and_missing():
    files = [
        {"download_url": "https://x/1.jpg"},
        {"download_url": ""},
        {"download_url": "   "},
        {"file_id": "no-url"},
        {"download_url": None},
        {"download_url": 123},  # non-string
        "not-an-object",
    ]
    assert extract_download_urls(files) == ["https://x/1.jpg"]


def test_empty_and_none():
    assert extract_download_urls([]) == []
    assert extract_download_urls(None) == []


def test_preserves_order():
    files = [FakeFile("https://x/3.jpg"), FakeFile("https://x/1.jpg"), FakeFile("https://x/2.jpg")]
    assert extract_download_urls(files) == ["https://x/3.jpg", "https://x/1.jpg", "https://x/2.jpg"]
