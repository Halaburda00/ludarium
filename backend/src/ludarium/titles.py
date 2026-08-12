"""Display-side title handling. The matcher's normalisation is not here.

`sort_title` is what the grid orders by; `normalised_title` is `ludamatch`'s
output and arrives in M2. Keeping them apart keeps the licence boundary clean —
this file stays in Ludarium (AGPL), the matcher lives in an MIT library.
"""

# English only, and deliberately so: titles are requested from every platform in
# English (`l=english` on Steam) precisely so one rule can apply to all of them.
LEADING_ARTICLES = ("The ", "A ", "An ")


def sort_title(title: str) -> str:
    """Move a leading article to the end so "The Witcher 3" files under W.

    Library convention, not a normalisation: case and punctuation are left
    alone, because this value is displayed as well as sorted.
    """

    stripped = title.strip()
    for article in LEADING_ARTICLES:
        if stripped.startswith(article):
            rest = stripped.removeprefix(article).strip()
            # "The " on its own is a title, not an article; without this the
            # result would be a lone comma.
            return f"{rest}, {article.strip()}" if rest else stripped
    return stripped
