import pytest

from ludarium.titles import sort_title


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("The Witcher 3: Wild Hunt", "Witcher 3: Wild Hunt, The"),
        ("A Plague Tale: Innocence", "Plague Tale: Innocence, A"),
        ("An Untitled Story", "Untitled Story, An"),
        ("Portal 2", "Portal 2"),
        ("  Hades  ", "Hades"),
        # Not an article: the word only counts when something follows it.
        ("The", "The"),
        # Nor is it one here — "Theme" merely starts with the same letters.
        ("Theme Hospital", "Theme Hospital"),
        # Case is left alone: this value is displayed as well as sorted.
        ("the last of us", "the last of us"),
    ],
)
def test_leading_articles_move_to_the_end(title: str, expected: str) -> None:
    assert sort_title(title) == expected
