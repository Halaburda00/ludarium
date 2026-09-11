"""Display-side title handling. The matcher's normalisation is not here.

`sort_title` is what a person reads and may set by hand; `sort_key` is what the
database compares. `normalised_title` is `ludamatch`'s output and arrives in M2.
Keeping them apart keeps the licence boundary clean — this file stays in
Ludarium (AGPL), the matcher lives in an MIT library.
"""

import unicodedata
from typing import Final

# English only, and deliberately so: titles are requested from every platform in
# English (`l=english` on Steam) precisely so one rule can apply to all of them.
LEADING_ARTICLES = ("The ", "A ", "An ")

# Removed before anything else rather than left to normalisation: NFKD spells
# `™` as the letters `TM`, so "Batman™: Arkham Knight" would fold to "batmantm:"
# and still file apart from "Batman: Arkham Asylum". A publisher adds these to a
# name; they are not part of which name it is.
MARKS: Final = str.maketrans("", "", "™®©℠")


def sort_title(title: str) -> str:
    """Move a leading article to the end so "The Witcher 3" files under W.

    Library convention, not a normalisation: case and punctuation are left
    alone, because a user may set this value by hand and rule 3 keeps what they
    wrote. What the database orders by is `sort_key`, derived from it.
    """

    stripped = title.strip()
    for article in LEADING_ARTICLES:
        if stripped.startswith(article):
            rest = stripped.removeprefix(article).strip()
            # "The " on its own is a title, not an article; without this the
            # result would be a lone comma.
            return f"{rest}, {article.strip()}" if rest else stripped
    return stripped


def sort_key(value: str) -> str:
    """`sort_title` as the grid compares it, which is not how anyone reads it.

    Case, accents, trademark signs and runs of whitespace are folded, so "ARC
    Raiders" files after "Amnesia" and "Brütal Legend" beside "Brutal Legend".
    Punctuation stays: it is what orders "Company of Heroes: Opposing Fronts"
    after "Company of Heroes 2", and stripping it is matcher normalisation,
    which belongs to `ludamatch`.

    Computed here rather than by the database, because neither engine can be
    asked for the same fold (ADR-0018).
    """

    decomposed = unicodedata.normalize("NFKD", value.translate(MARKS))
    # Decomposed again after casefolding: the two do not commute for every
    # character, and this is the order Unicode gives for a caseless comparison.
    folded = unicodedata.normalize("NFKD", decomposed.casefold())
    bare = "".join(character for character in folded if not unicodedata.combining(character))
    return " ".join(bare.split())
