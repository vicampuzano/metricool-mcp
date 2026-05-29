"""Tests for chatgpt_files.coerce_media_items (mixed media → URLs)."""

from dataclasses import dataclass

from chatgpt_files import coerce_media_items


@dataclass
class FakeFile:
    download_url: str | None = None


def test_plain_url_strings_pass_through():
    assert coerce_media_items(["https://x/1.jpg", "https://x/2.jpg"]) == [
        "https://x/1.jpg",
        "https://x/2.jpg",
    ]


def test_file_objects_as_dicts():
    media = [
        {"download_url": "https://x/1.jpg", "file_id": "a"},
        {"download_url": "https://x/2.jpg", "file_id": "b"},
    ]
    assert coerce_media_items(media) == ["https://x/1.jpg", "https://x/2.jpg"]


def test_file_objects_as_objects():
    assert coerce_media_items([FakeFile("https://x/1.jpg")]) == ["https://x/1.jpg"]


def test_mixed_strings_and_objects_preserve_order():
    media = [
        "https://url/a.jpg",
        {"download_url": "https://chatgpt/b.jpg", "file_id": "f1"},
        "https://url/c.jpg",
    ]
    assert coerce_media_items(media) == [
        "https://url/a.jpg",
        "https://chatgpt/b.jpg",
        "https://url/c.jpg",
    ]


def test_skips_blank_and_missing():
    media = [
        "https://x/1.jpg",
        "",
        "   ",
        {"download_url": ""},
        {"download_url": None},
        {"download_url": 123},  # non-string
        {"file_id": "no-url"},
        None,
        42,
    ]
    assert coerce_media_items(media) == ["https://x/1.jpg"]


def test_empty_and_none():
    assert coerce_media_items([]) == []
    assert coerce_media_items(None) == []
