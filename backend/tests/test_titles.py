import pytest

from ludarium.titles import sort_key, sort_title


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
        # Case is left alone: a user may set this by hand, and rule 3 keeps it.
        ("the last of us", "the last of us"),
    ],
)
def test_leading_articles_move_to_the_end(title: str, expected: str) -> None:
    assert sort_title(title) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The case that was found: byte order filed this ahead of "Amnesia".
        ("ARC Raiders", "arc raiders"),
        # Stripped before decomposition, which would otherwise spell it `TM`.
        ("Batman™: Arkham Knight", "batman: arkham knight"),
        ("Darkest Dungeon®", "darkest dungeon"),
        ("Brütal Legend", "brutal legend"),
        # Casefolded rather than lowered: `ß` compares as the two letters it is.
        ("Straße", "strasse"),
        # A compatibility character compares as the letters it stands for.
        ("ﬁnal ﬁght", "final fight"),
        # Both arrived from Steam like this.
        ("Gauntlet™ ", "gauntlet"),
        ("Yu-Gi-Oh!  Master Duel", "yu-gi-oh! master duel"),
        # Punctuation stays: that is ordering, and stripping it is the matcher's.
        ("Company of Heroes: Opposing Fronts", "company of heroes: opposing fronts"),
    ],
)
def test_the_key_folds_what_ordering_should_not_see(value: str, expected: str) -> None:
    assert sort_key(value) == expected


def test_a_trademark_sign_no_longer_splits_a_series() -> None:
    """The regression in the shape it was found in: one series, two blocks."""

    shuffled = [
        "Batman™: Arkham Origins",
        "Batman: Arkham City GOTY",
        "Batman™: Arkham Knight",
        "Batman: Arkham Asylum GOTY Edition",
    ]

    assert sorted(shuffled, key=sort_key) == [
        "Batman: Arkham Asylum GOTY Edition",
        "Batman: Arkham City GOTY",
        "Batman™: Arkham Knight",
        "Batman™: Arkham Origins",
    ]
