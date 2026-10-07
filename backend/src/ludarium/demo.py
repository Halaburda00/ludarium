"""A made-up library, for seeing Ludarium without connecting an account (#99).

Every title, year, genre and summary here is invented, so nothing in it comes
from IGDB or RAWG, and no account in it holds a credential. There are no scores
and no store links: a score is only served with the page it links to, and a
game that does not exist has no page (ADR-0034).

The library is built the way a user's is: synced through `sync_account`, added
through the manual entry endpoint, and decided about through the state, queue
and saved view endpoints. A shortcut that wrote the rows directly could store a
state the API itself would refuse, and the demo would show a library no user
can have.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api import manual, views, works
from ludarium.enums import EntityType, ItemKind, OwnershipType, PlayStatus, SourceKind
from ludarium.models import Account, Genre, Provider, SavedView, UserSession, Work, WorkGenre
from ludarium.models.types import ScalarValue
from ludarium.providers.base import FetchedLibrary, LibraryItem
from ludarium.resolver import record_many, resolve
from ludarium.sync import sync_account

# The `source_ref` of everything the dataset says about a work, where a real
# library would have IGDB's.
SOURCE: Final = "demo"
# The external id of each demo account. No Steam or Epic account id looks like
# this, which is what tells a demo database from a real one.
EXTERNAL_ID: Final = "demo"
LABEL: Final = "Demo"


class DemoRefusedError(RuntimeError):
    """The database holds something other than the demo, and the demo will not touch it."""


@dataclass(frozen=True, slots=True)
class Game:
    item_id: str
    title: str
    released: date
    genres: tuple[str, ...]
    summary: str
    kind: ItemKind = ItemKind.GAME
    # The `item_id` of the game an add-on belongs to, on the same account.
    parent: str | None = None
    ownership: OwnershipType = OwnershipType.OWNED
    playtime_minutes: int | None = None
    last_played: date | None = None

    def item(self) -> LibraryItem:
        return LibraryItem(
            provider_item_id=self.item_id,
            title=self.title,
            ownership_type=self.ownership,
            item_kind=self.kind,
            playtime_minutes=self.playtime_minutes,
            last_played_at=_moment(self.last_played),
            parent_item_id=self.parent,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """What the user decided about one work, as the state endpoint takes it."""

    title: str
    state: works.StateUpdate


GENRES: Final = {
    "role-playing": "Role-playing",
    "strategy": "Strategy",
    "tactics": "Tactics",
    "adventure": "Adventure",
    "puzzle": "Puzzle",
    "simulation": "Simulation",
    "platformer": "Platformer",
    "shooter": "Shooter",
    "racing": "Racing",
    "arcade": "Arcade",
    "indie": "Indie",
}

STEAM: Final = (
    Game(
        "lanterns-of-vael",
        "Lanterns of Vael",
        date(2019, 3, 14),
        ("role-playing", "adventure"),
        "A lamplighter walks a drowned kingdom back into the light, one district at a time.",
        playtime_minutes=4210,
        last_played=date(2026, 5, 2),
    ),
    Game(
        "lanterns-of-vael-ember-coast",
        "Lanterns of Vael: The Ember Coast",
        date(2020, 6, 9),
        ("role-playing", "adventure"),
        "A volcanic coastline, three new districts and the lamplighter's oldest rival.",
        kind=ItemKind.DLC,
        parent="lanterns-of-vael",
        playtime_minutes=780,
        last_played=date(2026, 5, 2),
    ),
    Game(
        "ironbark-frontier",
        "Ironbark Frontier",
        date(2021, 9, 30),
        ("strategy", "simulation"),
        "Settle a forest that fights back, and decide which of it is worth keeping.",
        playtime_minutes=2955,
        last_played=date(2026, 9, 28),
    ),
    Game(
        "ironbark-frontier-rivers",
        "Ironbark Frontier: Rivers & Roads",
        date(2022, 11, 15),
        ("strategy", "simulation"),
        "Barges, bridges and the toll disputes that come with them.",
        kind=ItemKind.DLC,
        parent="ironbark-frontier",
    ),
    Game(
        "the-quiet-orbit",
        "The Quiet Orbit",
        date(2017, 10, 3),
        ("adventure", "puzzle"),
        "The last engineer on a silent station works out why everyone else left.",
        playtime_minutes=690,
        last_played=date(2025, 2, 11),
    ),
    Game(
        "saltmarsh-courier",
        "Saltmarsh Courier",
        date(2022, 4, 21),
        ("simulation", "indie"),
        "Deliver letters across tidal flats before the sea takes the road back.",
    ),
    Game(
        "ninefold-gate",
        "Ninefold Gate",
        date(2015, 7, 8),
        ("platformer",),
        "Nine towers, one gate, and a jump that is always slightly too far.",
        playtime_minutes=245,
        last_played=date(2024, 8, 19),
    ),
    Game(
        "copperline-express",
        "Copperline Express",
        date(2020, 2, 27),
        ("simulation",),
        "Run a mountain railway through four seasons and one very stubborn goat.",
        playtime_minutes=1320,
        last_played=date(2025, 12, 30),
    ),
    Game(
        "hollow-tide",
        "Hollow Tide",
        date(2018, 11, 6),
        ("shooter", "adventure"),
        "A salvage diver fights through a flooded city that was never on any map.",
        playtime_minutes=410,
        last_played=date(2026, 1, 17),
    ),
    Game(
        "mirelight",
        "Mirelight",
        date(2023, 8, 24),
        ("puzzle", "indie"),
        "Bend reflected light through a marsh to wake the creatures sleeping in it.",
    ),
    Game(
        "mirelight-soundtrack",
        "Mirelight Original Soundtrack",
        date(2023, 8, 24),
        ("puzzle", "indie"),
        "Thirty-one tracks of reeds, rain and a very quiet cello.",
        kind=ItemKind.SOUNDTRACK,
    ),
    Game(
        "kestrel-squadron",
        "Kestrel Squadron",
        date(2016, 5, 12),
        ("shooter", "simulation"),
        "Fly the last propeller squadron in a war that has already moved on.",
        playtime_minutes=95,
        last_played=date(2023, 6, 4),
    ),
    Game(
        "ashen-cartography",
        "Ashen Cartography",
        date(2024, 10, 1),
        ("strategy",),
        "Map a continent after the fire, and argue with every faction about the borders.",
    ),
    Game(
        "paper-lighthouse",
        "Paper Lighthouse",
        date(2012, 1, 19),
        ("adventure", "indie"),
        "A folded-paper keeper guides ships home through a storm of origami.",
        playtime_minutes=530,
        last_played=date(2022, 12, 26),
    ),
    Game(
        "gloamwood-tactics",
        "Gloamwood Tactics",
        date(2019, 11, 20),
        ("tactics", "role-playing"),
        "A mercenary company takes contracts in a wood where the dusk lasts all day.",
        playtime_minutes=1840,
        last_played=date(2026, 10, 1),
    ),
    Game(
        "starfall-arcade",
        "Starfall Arcade",
        date(2010, 6, 1),
        ("arcade",),
        "Twelve cabinets of falling stars, a high score table and nothing else.",
        playtime_minutes=12,
        last_played=date(2021, 3, 9),
    ),
    Game(
        "brine-and-bramble",
        "Brine & Bramble",
        date(2021, 5, 18),
        ("simulation", "indie"),
        "Keep a seaside herb garden alive through salt winds and visiting goats.",
    ),
    Game(
        "thorncrown-saga",
        "Thorncrown Saga",
        date(2014, 9, 23),
        ("role-playing",),
        "Four heirs, one crown grown from thorns, and a succession nobody wins cleanly.",
        playtime_minutes=3605,
        last_played=date(2023, 1, 8),
    ),
    Game(
        "velvet-circuit",
        "Velvet Circuit",
        date(2025, 3, 6),
        ("racing", "arcade"),
        "Night races through a city of neon tunnels, with the lights as the track.",
        playtime_minutes=150,
        last_played=date(2026, 7, 21),
    ),
    Game(
        "velvet-circuit-demo",
        "Velvet Circuit Demo",
        date(2024, 11, 14),
        ("racing", "arcade"),
        "Two tracks and one car from the full game.",
        kind=ItemKind.DEMO,
        ownership=OwnershipType.FREE,
        playtime_minutes=40,
        last_played=date(2024, 11, 15),
    ),
)

# On the first run and gone from the second, as a game a platform stops listing
# is: it stays visible, in the removed view, to be restored (rule 1).
GONE: Final = Game(
    "starlit-harbor",
    "Starlit Harbor",
    date(2018, 4, 2),
    ("simulation",),
    "Run a small port that only opens at night, for ships that should not exist.",
    playtime_minutes=60,
    last_played=date(2022, 7, 14),
)

EPIC: Final = (
    Game(
        "driftglass",
        "Driftglass",
        date(2020, 12, 17),
        ("puzzle",),
        "Sort sea glass by colour until the tide pools reveal what they were hiding.",
        ownership=OwnershipType.FREE,
    ),
    Game(
        "rookery-heights",
        "Rookery Heights",
        date(2022, 6, 30),
        ("strategy", "simulation"),
        "Build a city for crows on the rooftops of a city for people.",
        playtime_minutes=1105,
        last_played=date(2026, 9, 12),
    ),
    Game(
        "sundial-run",
        "Sundial Run",
        date(2019, 8, 15),
        ("platformer", "indie"),
        "A shadow races around a sundial, one hour per level, and noon is a wall.",
        ownership=OwnershipType.FREE,
    ),
    Game(
        "cinder-vale",
        "Cinder Vale",
        date(2021, 2, 4),
        ("role-playing", "adventure"),
        "A blacksmith's apprentice forges the weapons for a war they do not believe in.",
        playtime_minutes=2470,
        last_played=date(2025, 6, 22),
    ),
    Game(
        "cinder-vale-frostbound",
        "Cinder Vale: Frostbound",
        date(2021, 12, 9),
        ("role-playing", "adventure"),
        "The war moves north, and the forge has to learn to work in the cold.",
        kind=ItemKind.DLC,
        parent="cinder-vale",
        playtime_minutes=640,
        last_played=date(2025, 7, 3),
    ),
    Game(
        "tidecaller-chronicles",
        "Tidecaller Chronicles",
        date(2016, 3, 29),
        ("adventure",),
        "An island cartographer learns the tides answer to whoever names them.",
        ownership=OwnershipType.FREE,
    ),
)

# Copies no platform knows about, as the manual entry form takes them.
MANUAL: Final = (
    manual.ManualEntry(
        title="Eastward Bells",
        store_label="PS5 disc",
        ownership_type=OwnershipType.PHYSICAL,
        release_year=2020,
    ),
    manual.ManualEntry(title="Small Hours", store_label="itch.io", release_year=2018),
)

# In the order they are made, which for the queued ones is the queue's order.
DECISIONS: Final = (
    Decision(
        "Lanterns of Vael",
        works.StateUpdate(
            play_status=PlayStatus.COMPLETED,
            rating=9,
            is_favourite=True,
            notes="Finished the Ember Coast too. The lighthouse district is the best level.",
        ),
    ),
    Decision("Ironbark Frontier", works.StateUpdate(play_status=PlayStatus.PLAYING)),
    Decision("Gloamwood Tactics", works.StateUpdate(play_status=PlayStatus.PLAYING, rating=8)),
    Decision("Rookery Heights", works.StateUpdate(play_status=PlayStatus.PLAYING)),
    Decision("The Quiet Orbit", works.StateUpdate(play_status=PlayStatus.COMPLETED, rating=8)),
    Decision(
        "Thorncrown Saga",
        works.StateUpdate(play_status=PlayStatus.MASTERED, rating=10, is_favourite=True),
    ),
    Decision("Paper Lighthouse", works.StateUpdate(play_status=PlayStatus.COMPLETED, rating=7)),
    Decision(
        "Ninefold Gate",
        works.StateUpdate(
            play_status=PlayStatus.DROPPED,
            rating=4,
            notes="Stuck on the sixth tower. Maybe one day.",
        ),
    ),
    Decision("Hollow Tide", works.StateUpdate(play_status=PlayStatus.ON_HOLD)),
    Decision("Cinder Vale", works.StateUpdate(play_status=PlayStatus.ON_HOLD, rating=7)),
    Decision("Mirelight", works.StateUpdate(play_status=PlayStatus.QUEUED)),
    Decision("Saltmarsh Courier", works.StateUpdate(play_status=PlayStatus.QUEUED)),
    Decision("Ashen Cartography", works.StateUpdate(play_status=PlayStatus.QUEUED)),
    Decision("Eastward Bells", works.StateUpdate(play_status=PlayStatus.QUEUED)),
    Decision("Starfall Arcade", works.StateUpdate(is_hidden=True)),
)

# Saved views, as the library's address would carry them.
VIEWS: Final = (
    ("Started, not finished", "status=playing&status=on_hold&sort=last_played&order=desc"),
    ("Strategy and tactics", "genre=strategy&genre=tactics&sort=release_date&order=desc"),
    ("Most played", "playtime_min=600&sort=playtime&order=desc"),
)


class DemoLibrary:
    """A platform account that always answers with the same library."""

    renewed_secret: str | None = None

    def __init__(self, key: str, games: Sequence[Game]) -> None:
        self.key = key
        self._games = games

    async def validate_credentials(self) -> None:
        return None

    async def fetch_library(self) -> FetchedLibrary:
        return FetchedLibrary(items=[game.item() for game in self._games])


def visitor(user_id: int) -> UserSession:
    """The session every demo request runs as. Never stored: nobody signed in to make it."""

    return UserSession(user_id=user_id, token_hash="", expires_at=datetime.max.replace(tzinfo=UTC))


async def seed_demo(session: AsyncSession, *, user_id: int) -> bool:
    """Make the demo library on an empty database. True if it was made now.

    A database that already holds the whole demo is left as it is. Anything
    else is refused, so a real instance started with `LUDARIUM_DEMO` by mistake
    fails to start instead of being shown to anyone who opens it.
    """

    accounts = set(
        await session.execute(
            select(Provider.key, Account.external_account_id).join(
                Provider, Provider.id == Account.provider_id
            )
        )
    )
    held = await session.scalar(select(func.count()).select_from(Work))
    if not accounts and not held:
        await _seed(session, user_id=user_id)
        return True
    demo = {("steam", EXTERNAL_ID), ("epic", EXTERNAL_ID), (manual.MANUAL, None)}
    if accounts != demo:
        raise DemoRefusedError(
            "LUDARIUM_DEMO is set, but the database holds a library of its own; "
            "point the demo at an empty database"
        )
    # Saved views are the last thing made, so a demo without them is one whose
    # seeding stopped half-way, and showing it would show a broken library.
    if not await session.scalar(select(func.count()).select_from(SavedView)):
        raise DemoRefusedError(
            "the demo database was left half-made by an earlier start; delete it and start again"
        )
    return False


async def _seed(session: AsyncSession, *, user_id: int) -> None:
    record = visitor(user_id)
    providers = {
        provider.key: provider
        for provider in await session.scalars(
            select(Provider).where(Provider.key.in_(("steam", "epic")))
        )
    }
    steam, epic = (
        Account(
            user_id=user_id,
            provider_id=providers[key].id,
            external_account_id=EXTERNAL_ID,
            label=LABEL,
        )
        for key in ("steam", "epic")
    )
    session.add_all([steam, epic])
    await session.commit()

    await sync_account(session, account=steam, library=DemoLibrary("steam", (*STEAM, GONE)))
    await sync_account(session, account=steam, library=DemoLibrary("steam", STEAM))
    await sync_account(session, account=epic, library=DemoLibrary("epic", EPIC))
    for entry in MANUAL:
        await manual.add(entry, session, record)

    rows = await session.execute(select(Work.id, Work.title))
    by_title = {title: work_id for work_id, title in rows}
    await _describe(session, by_title)
    for decision in DECISIONS:
        await works.update_state(by_title[decision.title], decision.state, session, record)
    for name, query in VIEWS:
        await views.create(views.ViewCreate(name=name, query=query), session, record)


async def _describe(session: AsyncSession, by_title: dict[str, int]) -> None:
    """What IGDB would say about each synced game: its year, date, summary and genres."""

    genres = {slug: Genre(slug=slug, name=name) for slug, name in GENRES.items()}
    session.add_all(genres.values())
    await session.flush()
    for game in (*STEAM, GONE, *EPIC):
        work_id = by_title[game.title]
        values: dict[str, ScalarValue | None] = {
            "release_year": game.released.year,
            "release_date": game.released.isoformat(),
            "summary": game.summary,
        }
        recorded = await record_many(
            session,
            entity_type=EntityType.WORK,
            entity_id=work_id,
            source_kind=SourceKind.METADATA_PROVIDER,
            source_ref=SOURCE,
            values=values,
        )
        await resolve(
            session,
            entity_type=EntityType.WORK,
            entity_id=work_id,
            fields=list(values),
            recorded=recorded,
        )
        session.add_all(
            WorkGenre(work_id=work_id, genre_id=genres[slug].id, source_ref=SOURCE)
            for slug in game.genres
        )
    await session.commit()


def _moment(day: date | None) -> datetime | None:
    if day is None:
        return None
    # Noon, so that the day reads as the same date anywhere but the date line.
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
