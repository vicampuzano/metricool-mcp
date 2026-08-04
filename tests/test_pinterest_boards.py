"""Pinterest board id resolution (mirrors Java PinterestBoardIdResolver)."""

import pytest

from pinterest_boards import (
    PinterestBoardResolutionError,
    resolve_board_id,
    resolve_pinterest_board,
)


class FakeClient:
    def __init__(self, boards):
        self.boards = boards
        self.calls = 0

    def get_pinterest_boards(self, brand_id):
        self.calls += 1
        return self.boards


BOARDS = [
    {"id": "12345", "name": "Recetas de Verano"},
    {"id": "67890", "name": "Marketing Tips"},
]


def test_numeric_id_passes_through_without_a_call():
    client = FakeClient(BOARDS)
    assert resolve_board_id("12345", "1", client) == "12345"
    assert client.calls == 0


def test_name_is_resolved_to_id():
    assert resolve_board_id("Marketing Tips", "1", FakeClient(BOARDS)) == "67890"


def test_name_match_is_case_and_whitespace_insensitive():
    assert resolve_board_id("  marketing TIPS ", "1", FakeClient(BOARDS)) == "67890"


def test_url_encoded_name_is_decoded():
    assert resolve_board_id("Recetas%20de%20Verano", "1", FakeClient(BOARDS)) == "12345"


def test_unknown_name_lists_available_boards():
    with pytest.raises(PinterestBoardResolutionError) as exc:
        resolve_board_id("Nope", "1", FakeClient(BOARDS))
    message = str(exc.value)
    assert "id=12345" in message and "Marketing Tips" in message
    assert "Retry using the numeric board id" in message


def test_ambiguous_name_is_rejected():
    dupes = [{"id": "1", "name": "Same"}, {"id": "2", "name": "same"}]
    with pytest.raises(PinterestBoardResolutionError):
        resolve_board_id("Same", "1", FakeClient(dupes))


# --- in-place post_info resolution ---


def test_post_info_board_name_is_replaced_by_id():
    info = {"pinterestData": {"boardId": "Marketing Tips", "pinTitle": "t"}}
    resolve_pinterest_board(info, "1", FakeClient(BOARDS))
    assert info["pinterestData"]["boardId"] == "67890"


def test_post_without_pinterest_data_makes_no_call():
    client = FakeClient(BOARDS)
    info = {"providers": [{"network": "twitter"}]}
    resolve_pinterest_board(info, "1", client)
    assert client.calls == 0


def test_blank_board_id_is_left_for_the_validator():
    client = FakeClient(BOARDS)
    info = {"pinterestData": {"boardId": ""}}
    resolve_pinterest_board(info, "1", client)
    assert client.calls == 0
    assert info["pinterestData"]["boardId"] == ""
