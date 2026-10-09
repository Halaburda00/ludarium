"""A library kept in a file: a spreadsheet, a JSON list, another tool's export (#130, ADR-0039).

Read here, in the backend, into one ingest report per platform the file names,
so that a file goes down the same path as any other report (rule 8). What this
module decides is only what the file says: which rows it could read, where each
copy belongs, and what key a re-import finds it under.
"""

import codecs
import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any, Final

from ludarium.enums import ItemKind, OwnershipType
from ludarium.filters import FIRST_YEAR, LAST_YEAR, MAX_MINUTES
from ludarium.ingest import MAX_ITEMS

# A spreadsheet of every game anyone owns is a few MB; this is a bound on what
# one upload can make us hold, not a guess at a library.
MAX_BYTES: Final = 16 * 1024 * 1024
MAX_ROWS: Final = MAX_ITEMS
MAX_TITLE: Final = 1024
# Room for the `id:` prefix inside the contract's 256.
MAX_ID: Final = 200
MAX_PLATFORM: Final = 100

COLUMNS: Final = (
    "id",
    "title",
    "platform",
    "ownership_type",
    "item_kind",
    "playtime_minutes",
    "acquired_at",
    "release_year",
)
DELIMITERS: Final = (",", ";", "\t")

# What a file may call a platform, folded, and the provider it means. Only
# names a person would write; anything else lands under `other`, labelled with
# what the file said, so nothing is lost by a name missing here.
PLATFORMS: Final[dict[str, str]] = {
    name: key
    for key, names in {
        "steam": ("steam",),
        "epic": ("epic", "epic games", "epic games store", "egs"),
        "gog": ("gog", "gog.com", "gog galaxy"),
        "ea": ("ea", "ea app", "ea play", "origin", "electronic arts"),
        "ubisoft": ("ubisoft", "ubisoft connect", "uplay", "ubi"),
        "battlenet": ("battle.net", "battlenet", "blizzard"),
        "xbox": ("xbox", "xbox app", "microsoft store", "windows store"),
        "playstation": ("playstation", "psn", "ps3", "ps4", "ps5", "ps vita"),
        "nintendo": ("nintendo", "switch", "nintendo switch", "eshop", "nintendo eshop"),
        "itch": ("itch", "itch.io"),
        "humble": ("humble", "humble bundle", "humble store"),
        "amazon": ("amazon", "amazon games", "prime gaming"),
    }.items()
    for name in names
}
OTHER: Final = "other"
# The label of the account rows with no platform go to. Data, not UI text: the
# user renames it on the accounts screen like any other.
NO_PLATFORM: Final = "Imported"


class UnreadableFileError(Exception):
    """The file as a whole cannot be read, so nothing in it is imported."""


@dataclass(frozen=True, slots=True)
class Problem:
    """A row that could not be read, by its number as a spreadsheet shows it."""

    row: int
    message: str


@dataclass(frozen=True, slots=True)
class Row:
    """One copy the file describes, read and checked."""

    row: int
    title: str
    # As the file wrote it, stripped. Blank is "no platform".
    platform: str
    id: str | None = None
    ownership_type: OwnershipType = OwnershipType.OWNED
    item_kind: ItemKind | None = None
    playtime_minutes: int | None = None
    acquired_at: datetime | None = None
    release_year: int | None = None


@dataclass(frozen=True, slots=True)
class Group:
    """The rows bound for one account: one platform as the file named it."""

    provider: str
    external_account_id: str
    label: str
    # Each row with the key a re-import finds it under.
    items: list[tuple[str, Row]]


@dataclass(slots=True)
class ParsedFile:
    format: str
    encoding: str | None = None
    delimiter: str | None = None
    columns: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def parse(data: bytes, filename: str) -> ParsedFile:
    """Read a `.csv` or `.json` file, refusing it whole when its shape is wrong.

    A row with a bad value costs that row and is reported; a file whose
    encoding, structure or header cannot be read costs the file, because
    guessing at those is how half an import happens.
    """

    if len(data) > MAX_BYTES:
        raise UnreadableFileError(f"the file is over {MAX_BYTES // (1024 * 1024)} MB")
    suffix = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if suffix == "json":
        return _json(data)
    if suffix in ("csv", "tsv", "txt"):
        return _csv(data)
    raise UnreadableFileError("expected a .csv or a .json file")


def group(parsed: ParsedFile) -> list[Group]:
    """The rows by account, each with a key that a re-import of the same file finds again.

    A row's `id` is the key when it has one. Without one the key is its title,
    folded, and how many rows before it in the same account had that title:
    two copies of one game stay two copies, and the same file imported twice
    lands on the same rows. Reordering duplicates of one title swaps which
    copy is which, which is harmless while they are alike.

    An `id` used twice on one platform is a problem with the later row: the two
    would be one copy, and which of them wins would be an accident of order.
    """

    groups: dict[str, Group] = {}
    seen: dict[tuple[str, str], int] = {}
    ids: dict[tuple[str, str], int] = {}
    for row in parsed.rows:
        provider, account, label = _account(row.platform)
        if row.id is not None:
            first = ids.setdefault((account, row.id), row.row)
            if first != row.row:
                parsed.problems.append(Problem(row.row, f"`id` {row.id!r} is on row {first} too"))
                continue
        target = groups.setdefault(account, Group(provider, account, label, []))
        if row.id is not None:
            key = f"id:{row.id}"
        else:
            folded = _fold(row.title)
            count = seen[account, folded] = seen.get((account, folded), 0) + 1
            digest = hashlib.sha256(folded.encode()).hexdigest()[:24]
            key = f"title:{digest}" + (f"#{count}" if count > 1 else "")
        target.items.append((key, row))
    return list(groups.values())


def _account(platform: str) -> tuple[str, str, str]:
    """The provider a platform name means, the account's id under it, and its label."""

    folded = _fold(platform)
    provider = PLATFORMS.get(folded, OTHER)
    # The folded name rather than the provider: "EA" and "Origin" in one file
    # are two lists the user kept apart, and they stay two accounts.
    return provider, f"import:{folded}", platform or NO_PLATFORM


def _fold(text: str) -> str:
    # Deliberately not `titles.search_key`, which may change with the Unicode
    # database and is healed on start (`seed.reconcile_folded_keys`). A key
    # here lives in rows a later import has to find again.
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _decode(data: bytes) -> tuple[str, str]:
    """The text, and what it was decoded from.

    UTF-8 with or without a BOM, UTF-16 with one (Excel's "Unicode text"), and
    Windows-1250, which is what Excel writes a CSV in on a Polish Windows.
    Anything that is none of them is refused rather than read as mojibake.
    """

    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as exc:
            raise UnreadableFileError("the file says UTF-16 but is not") from exc
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1250"), "windows-1250"
    except UnicodeDecodeError as exc:
        raise UnreadableFileError(
            "the file is neither UTF-8, UTF-16 nor Windows-1250; save it as UTF-8"
        ) from exc


def _csv(data: bytes) -> ParsedFile:
    text, encoding = _decode(data)
    if "\x00" in text:
        raise UnreadableFileError("the file holds NUL bytes, so it is not a text file")
    header = text.split("\n", 1)[0]
    # Counted on the header line, which carries no quoted field worth the name:
    # more predictable than `csv.Sniffer`, which guesses from the data rows too.
    delimiter = max(DELIMITERS, key=header.count)
    if not header.count(delimiter):
        delimiter = ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        lines = list(reader)
    except csv.Error as exc:
        raise UnreadableFileError(f"the file is not valid CSV: {exc}") from exc
    if not lines:
        raise UnreadableFileError("the file is empty")
    names = [_column(name) for name in lines[0]]
    columns, ignored = _columns(names)
    parsed = ParsedFile(
        format="csv", encoding=encoding, delimiter=delimiter, columns=columns, ignored=ignored
    )
    # Row 1 is the header, so the first data row is row 2, as a spreadsheet numbers it.
    body = [(number, values) for number, values in enumerate(lines[1:], start=2) if any(values)]
    _too_many(len(body))
    for number, values in body:
        if len(values) > len(names):
            parsed.problems.append(
                Problem(number, f"{len(values)} fields where the header has {len(names)}")
            )
            continue
        _read(parsed, number, dict(zip(names, values, strict=False)))
    return parsed


def _json(data: bytes) -> ParsedFile:
    # JSON is UTF-8 by its RFC; a BOM is tolerated, anything else is refused.
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnreadableFileError("a JSON file has to be UTF-8") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnreadableFileError(f"the file is not valid JSON: {exc}") from exc
    if not isinstance(document, list):
        raise UnreadableFileError("expected a JSON array of objects, one per game")
    _too_many(len(document))
    names: dict[str, None] = {}
    for entry in document:
        if isinstance(entry, dict):
            names.update(dict.fromkeys(_column(str(key)) for key in entry))
    columns, ignored = _columns(list(names))
    parsed = ParsedFile(format="json", encoding="utf-8", columns=columns, ignored=ignored)
    # Numbered from 1, the way a person counts the entries of a list.
    for number, entry in enumerate(document, start=1):
        if not isinstance(entry, dict):
            parsed.problems.append(Problem(number, "not an object"))
            continue
        _read(parsed, number, {_column(str(key)): value for key, value in entry.items()})
    return parsed


def _too_many(count: int) -> None:
    if count > MAX_ROWS:
        raise UnreadableFileError(f"the file has {count} rows; at most {MAX_ROWS} are read")


def _column(name: str) -> str:
    return re.sub(r"[\s-]+", "_", name.strip().lstrip("﻿").casefold())


def _columns(names: list[str]) -> tuple[list[str], list[str]]:
    duplicated = sorted({name for name in names if name and names.count(name) > 1})
    if duplicated:
        raise UnreadableFileError(f"a column appears twice: {', '.join(duplicated)}")
    if "title" not in names:
        raise UnreadableFileError("a `title` column is required")
    known: list[str] = [name for name in COLUMNS if name in names]
    ignored = [name for name in names if name and name not in COLUMNS]
    return known, ignored


def _read(parsed: ParsedFile, number: int, values: dict[str, Any]) -> None:
    try:
        parsed.rows.append(_row(number, values))
    except ValueError as exc:
        parsed.problems.append(Problem(number, str(exc)))


def _row(number: int, values: dict[str, Any]) -> Row:
    title = _text(values.get("title"), "title", MAX_TITLE)
    if title is None:
        raise ValueError("no title")
    return Row(
        row=number,
        title=title,
        platform=_text(values.get("platform"), "platform", MAX_PLATFORM) or "",
        id=_text(values.get("id"), "id", MAX_ID),
        ownership_type=_choice(values.get("ownership_type"), OwnershipType, "ownership_type")
        or OwnershipType.OWNED,
        item_kind=_choice(values.get("item_kind"), ItemKind, "item_kind"),
        playtime_minutes=_whole(values.get("playtime_minutes"), "playtime_minutes", 0, MAX_MINUTES),
        acquired_at=_moment(values.get("acquired_at")),
        release_year=_whole(values.get("release_year"), "release_year", FIRST_YEAR, LAST_YEAR),
    )


def _text(value: Any, name: str, longest: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"`{name}` is not text")
    text = str(value).strip()
    if len(text) > longest:
        raise ValueError(f"`{name}` is longer than {longest} characters")
    return text or None


def _choice[E: (OwnershipType, ItemKind)](value: Any, choices: type[E], name: str) -> E | None:
    text = _text(value, name, 64)
    if text is None:
        return None
    try:
        return choices(re.sub(r"[\s-]+", "_", text.casefold()))
    except ValueError:
        allowed = ", ".join(choice.value for choice in choices)
        raise ValueError(f"`{name}` is {text!r}; expected one of {allowed}") from None


def _whole(value: Any, name: str, least: int, most: int) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"`{name}` is not a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ValueError(f"`{name}` is {value!r}, not a whole number")
    if not least <= number <= most:
        raise ValueError(f"`{name}` is {number}; expected {least} to {most}")
    return number


# A date as a Polish spreadsheet writes it.
_DOTTED: Final = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _moment(value: Any) -> datetime | None:
    """A date or a moment: ISO 8601, or `DD.MM.YYYY`. A bare date or time is UTC."""

    text = _text(value, "acquired_at", 64)
    if text is None:
        return None
    try:
        if dotted := _DOTTED.fullmatch(text):
            day, month, year = (int(part) for part in dotted.groups())
            moment = datetime.combine(date(year, month, day), time())
        elif len(text) == 10:
            moment = datetime.combine(date.fromisoformat(text), time())
        else:
            moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"`acquired_at` is {text!r}; expected YYYY-MM-DD, DD.MM.YYYY or an ISO 8601 time"
        ) from None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
