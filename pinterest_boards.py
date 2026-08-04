"""
Pinterest board id resolution.

The Metricool API needs a numeric board id, but LLMs routinely send the board
*name* instead — often URL-encoded, because that is how it appears in a Pinterest
URL. When that happens the post is rejected with an opaque backend error.

This module mirrors the Java PinterestBoardIdResolver (ES5MPTM3-5899): a value
that is already numeric passes straight through; anything else triggers a lazy
lookup of the brand's boards and is matched by normalized name (URL-decoded,
trimmed, case-insensitive). Zero or multiple matches raise
PinterestBoardResolutionError, whose message lists the available boards so the
model can retry with the id.

Depends only on the stdlib plus a duck-typed client exposing
``get_pinterest_boards(brand_id) -> list[dict]``, so it can be unit-tested in
isolation.
"""

import logging
import re
from urllib.parse import unquote

logger = logging.getLogger(__name__)

_NUMERIC_RE = re.compile(r"^\d+$")


class PinterestBoardResolutionError(Exception):
    """A Pinterest board reference could not be resolved to a numeric id.

    The message is what the MCP client sees, so it lists the available boards
    and tells the model to retry with the numeric id.
    """

    def __init__(self, board_id_or_name: str, available_boards: list[dict]) -> None:
        self.board_id_or_name = board_id_or_name
        self.available_boards = available_boards or []
        listed = ", ".join(
            f"{{id={b.get('id')}, name={b.get('name')}}}" for b in self.available_boards
        )
        super().__init__(
            f"Pinterest board '{board_id_or_name}' could not be resolved to a numeric "
            f"board ID (no match or multiple matches). Available boards: [{listed}]. "
            "Retry using the numeric board id."
        )


def is_numeric_board_id(value: object) -> bool:
    """True if the value is already the numeric board id the API expects."""
    return isinstance(value, str) and bool(_NUMERIC_RE.match(value))


def _normalize(value: str) -> str:
    """Normalize a board name for comparison: URL-decode, trim, lower-case."""
    try:
        decoded = unquote(value)
    except (ValueError, TypeError):
        decoded = value
    return decoded.strip().lower()


def resolve_board_id(board_id_or_name: object, brand_id: str, client) -> str:
    """Return the numeric Pinterest board id for a board id or name.

    Already-numeric values are returned untouched with no network call. Raises
    PinterestBoardResolutionError when the name matches zero or several boards.
    """
    if is_numeric_board_id(board_id_or_name):
        return board_id_or_name
    if not isinstance(board_id_or_name, str) or not board_id_or_name.strip():
        raise PinterestBoardResolutionError(str(board_id_or_name), [])

    boards = client.get_pinterest_boards(brand_id)
    target = _normalize(board_id_or_name)
    matches = [
        b for b in boards
        if isinstance(b.get("name"), str) and _normalize(b["name"]) == target
    ]
    if len(matches) == 1:
        resolved = str(matches[0].get("id"))
        logger.info(
            "resolved Pinterest board %r to id %s", board_id_or_name, resolved
        )
        return resolved
    logger.warning(
        "Pinterest board %r matched %d boards out of %d",
        board_id_or_name, len(matches), len(boards),
    )
    raise PinterestBoardResolutionError(board_id_or_name, boards)


def resolve_pinterest_board(post_info: dict, brand_id: str, client) -> None:
    """Resolve pinterestData.boardId in place, if the post targets Pinterest.

    A post without pinterestData, or with an already-numeric boardId, is left
    untouched and costs no network call.
    """
    pinterest_data = post_info.get("pinterestData")
    if not isinstance(pinterest_data, dict):
        return
    board = pinterest_data.get("boardId")
    if is_numeric_board_id(board):
        return
    # An empty boardId is the validator's job to report, with a clearer message.
    if not (isinstance(board, str) and board.strip()):
        return
    pinterest_data["boardId"] = resolve_board_id(board, brand_id, client)
