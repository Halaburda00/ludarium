import codecs
import json
from datetime import UTC, datetime

import pytest

from ludarium.enums import ItemKind, OwnershipType
from ludarium.imports import NO_PLATFORM, UnreadableFileError, group, parse


def test_a_csv_is_read_with_its_columns_named_loosely() -> None:
    parsed = parse(
        b"Title,Platform,Playtime Minutes,ownership-type,Notes\nGothic,GOG,600,physical,shelf\n",
        "games.csv",
    )

    (row,) = parsed.rows
    assert (row.title, row.platform, row.playtime_minutes, row.ownership_type) == (
        "Gothic",
        "GOG",
        600,
        OwnershipType.PHYSICAL,
    )
    assert parsed.columns == ["title", "platform", "ownership_type", "playtime_minutes"]
    # Shown in the preview, so a misspelt column is seen rather than lost.
    assert parsed.ignored == ["notes"]
    assert (parsed.encoding, parsed.delimiter) == ("utf-8", ",")


def test_a_polish_excel_file_is_read_in_its_own_encoding() -> None:
    # What Excel on a Polish Windows saves as "CSV (rozdzielany średnikami)".
    data = "title;platform;acquired_at\nWiedźmin 3;Pudełko;24.12.2015\n".encode("cp1250")

    parsed = parse(data, "gry.csv")

    (row,) = parsed.rows
    assert (row.title, row.platform) == ("Wiedźmin 3", "Pudełko")
    assert row.acquired_at == datetime(2015, 12, 24, tzinfo=UTC)
    assert (parsed.encoding, parsed.delimiter) == ("windows-1250", ";")


def test_utf8_with_a_bom_and_utf16_are_read() -> None:
    text = "title\tplatform\nŻółć\tSteam\n"

    with_bom = parse(codecs.BOM_UTF8 + text.encode(), "a.csv")
    utf16 = parse(text.encode("utf-16"), "a.txt")

    assert [row.title for row in with_bom.rows] == ["Żółć"]
    assert with_bom.columns == ["title", "platform"]
    assert ([row.title for row in utf16.rows], utf16.encoding) == (["Żółć"], "utf-16")


def test_a_quoted_newline_stays_inside_its_title() -> None:
    parsed = parse(b'title,platform\n"Two\nLines",EA\nNext,EA\n', "a.csv")

    assert [row.title for row in parsed.rows] == ["Two\nLines", "Next"]
    # Numbered as a spreadsheet numbers rows, not lines of text.
    assert [row.row for row in parsed.rows] == [2, 3]


@pytest.mark.parametrize(
    ("data", "name", "reason"),
    [
        (b"name,platform\nGothic,GOG\n", "a.csv", "`title` column is required"),
        (b"title,Title\nGothic,Gothic\n", "a.csv", "appears twice: title"),
        (b"title\n\x81\x83\n", "a.csv", "neither UTF-8"),
        (b"title\nGo\x00thic\n", "a.csv", "NUL"),
        (b'title\n"unterminated\n', "a.csv", "not valid CSV"),
        (b"", "a.csv", "empty"),
        (b'{"title": "Gothic"}', "a.json", "JSON array"),
        (b"[", "a.json", "not valid JSON"),
        ('[{"title": "Wiedźmin"}]'.encode("cp1250"), "a.json", "UTF-8"),
        (b"title\nGothic\n", "a.xlsx", ".csv or a .json"),
    ],
)
def test_a_file_that_cannot_be_read_is_refused_whole(data: bytes, name: str, reason: str) -> None:
    with pytest.raises(UnreadableFileError, match=reason):
        parse(data, name)


def test_a_bad_value_costs_its_row_and_says_why() -> None:
    parsed = parse(
        b"title,ownership_type,item_kind,playtime_minutes,release_year,acquired_at\n"
        b"Fine,owned,dlc,10,2001,2024-05-01\n"
        b",owned,,,,\n"
        b"A,rented,,,,\n"
        b"B,,expansion,,,\n"
        b"C,,,1.5,,\n"
        b"D,,,,1949,\n"
        b"E,,,,,May 2024\n"
        b"F,,,,,,extra\n",
        "a.csv",
    )

    assert [row.title for row in parsed.rows] == ["Fine"]
    assert parsed.rows[0].item_kind is ItemKind.DLC
    assert [(problem.row, problem.message.split(";")[0]) for problem in parsed.problems] == [
        (3, "no title"),
        (4, "`ownership_type` is 'rented'"),
        (5, "`item_kind` is 'expansion'"),
        (6, "`playtime_minutes` is '1.5', not a whole number"),
        (7, "`release_year` is 1949"),
        (8, "`acquired_at` is 'May 2024'"),
        (9, "7 fields where the header has 6"),
    ]


def test_a_json_list_is_read_with_typed_values() -> None:
    data = json.dumps(
        [
            {"title": "Gothic", "platform": "Origin", "playtime_minutes": 90, "id": 7},
            {"title": "Risen", "acquired_at": "2024-05-01T12:00:00+02:00", "rating": 5},
            "not an object",
        ]
    ).encode()

    parsed = parse(data, "library.json")

    assert [(row.title, row.id, row.playtime_minutes) for row in parsed.rows] == [
        ("Gothic", "7", 90),
        ("Risen", None, None),
    ]
    assert parsed.rows[1].acquired_at == datetime(2024, 5, 1, 10, tzinfo=UTC)
    assert parsed.ignored == ["rating"]
    assert [(problem.row, problem.message) for problem in parsed.problems] == [(3, "not an object")]


def test_rows_land_on_the_platform_their_name_means() -> None:
    parsed = parse(
        b"title,platform\nA,Origin\nB,EA app\nC,ps5\nD,Shelf in the hall\nE,\nF,  origin \n",
        "a.csv",
    )

    groups = {each.external_account_id: each for each in group(parsed)}

    assert {key: (each.provider, each.label) for key, each in groups.items()} == {
        # "EA" and "Origin" stay two lists, as the user kept them.
        "import:origin": ("ea", "Origin"),
        "import:ea app": ("ea", "EA app"),
        "import:ps5": ("playstation", "ps5"),
        "import:shelf in the hall": ("other", "Shelf in the hall"),
        "import:": ("other", NO_PLATFORM),
    }
    assert [row.title for _, row in groups["import:origin"].items] == ["A", "F"]


def test_two_copies_of_one_title_stay_two_and_keep_their_keys() -> None:
    data = b"title,platform\nDoom,Shelf\ndoom ,Shelf\nDoom,Other shelf\n"

    first = group(parse(data, "a.csv"))
    again = group(parse(data, "a.csv"))

    keys = [[key for key, _ in each.items] for each in first]
    assert keys == [[key for key, _ in each.items] for each in again]
    (shelf, other) = keys
    assert len(set(shelf)) == 2
    assert shelf[1] == f"{shelf[0]}#2"
    # Each account counts its own copies.
    assert other == [shelf[0]]


def test_an_id_is_the_key_and_a_repeated_one_is_a_problem() -> None:
    parsed = parse(b"id,title,platform\n1,A,Shelf\n1,B,Shelf\n1,C,Box\n", "a.csv")

    (shelf, box) = group(parsed)

    assert [(key, row.title) for key, row in shelf.items] == [("id:1", "A")]
    assert [(key, row.title) for key, row in box.items] == [("id:1", "C")]
    assert [(problem.row, problem.message) for problem in parsed.problems] == [
        (3, "`id` '1' is on row 2 too")
    ]
