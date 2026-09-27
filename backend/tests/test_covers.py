import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from conftest import make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_matching import TOKEN

from ludarium.covers import DIRECTORY, fetch_covers
from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import EntityType, ImageKind, SyncStatus
from ludarium.models import ExternalId, ImageAsset, Work
from ludarium.providers import IgdbClient, IgdbCredentials, MemoryTokenStore, RequestLimiter
from ludarium.providers import igdb as igdb_module
from ludarium.seed import seed_providers

# Invented ids, and bytes that only start like a JPEG: IGDB's images may not be
# redistributed, so none is recorded here.
COVERS_URL = f"{igdb_module.IGDB_API}/covers"


def jpeg(tag: str) -> bytes:
    return b"\xff\xd8\xff\xe0" + tag.encode()


class Cdn:
    """IGDB's `covers` endpoint and its image CDN, as the step meets them."""

    def __init__(self, covers: dict[int, str], missing: frozenset[str] = frozenset()) -> None:
        self.covers = covers
        self.missing = missing
        self.downloads: list[str] = []

    def answer_covers(self, request: httpx.Request) -> httpx.Response:
        found = re.search(r"game = \(([^)]*)\)", request.content.decode())
        assert found is not None
        games = [int(game) for game in found[1].split(",")]
        rows = [
            {"id": n, "game": game, "image_id": self.covers[game]}
            for n, game in enumerate(games, 1)
            if game in self.covers
        ]
        return httpx.Response(200, json=rows)

    def answer_image(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 2)
        self.downloads.append(f"{name[1]}/{name[2]}")
        image_id = name[2].removesuffix(".jpg")
        if f"{name[1]}/{image_id}" in self.missing:
            return httpx.Response(404)
        return httpx.Response(200, content=jpeg(f"{name[1]}/{image_id}"))

    def mount(self) -> "Cdn":
        respx.post(igdb_module.TWITCH_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
        respx.post(COVERS_URL).mock(side_effect=self.answer_covers)
        respx.get(url__startswith=igdb_module.IGDB_IMAGES).mock(side_effect=self.answer_image)
        return self


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(igdb_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(igdb_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def client() -> AsyncIterator[IgdbClient]:
    async with httpx.AsyncClient() as http:
        yield IgdbClient(
            IgdbCredentials(client_id="not-a-real-client-id", client_secret="not-a-real-secret"),
            http,
            tokens=MemoryTokenStore(),
            limiter=RequestLimiter(per_second=1000, open_at_once=1000),
        )


@pytest.fixture
async def seeded(session: AsyncSession) -> None:
    await seed_providers(session)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    # Not `tmp_path` itself: the test database lives there.
    return tmp_path / "data"


async def anchored(session: AsyncSession, game: int, title: str = "A game") -> Work:
    work = await make_work(session, title)
    work.igdb_id, work.is_matched = game, True
    session.add(
        ExternalId(
            entity_type=EntityType.WORK, entity_id=work.id, namespace="igdb", value=str(game)
        )
    )
    await session.flush()
    return work


async def run(db: Database, client: IgdbClient, data_dir: Path) -> SyncStatus:
    return (await enrich(db, provider="igdb", step=fetch_covers(client, data_dir))).status


async def covers_of(db: Database, work_id: int) -> list[tuple[int | None, str | None]]:
    async with db.session_factory() as reader:
        rows = await reader.scalars(
            select(ImageAsset)
            .where(ImageAsset.entity_id == work_id, ImageAsset.kind == ImageKind.COVER)
            .order_by(ImageAsset.width)
        )
        return [(image.width, image.local_path) for image in rows]


@respx.mock
async def test_both_sizes_of_a_matched_work_s_cover_land_on_disk_with_their_rows(
    db: Database, session: AsyncSession, seeded: None, client: IgdbClient, data_dir: Path
) -> None:
    Cdn({1942: "co1wyy"}).mount()
    work = await anchored(session, 1942)
    await session.commit()

    assert await run(db, client, data_dir) is SyncStatus.SUCCESS

    assert await covers_of(db, work.id) == [
        (264, "covers/igdb/co1wyy.jpg"),
        (528, "covers/igdb/co1wyy_2x.jpg"),
    ]
    assert (data_dir / "covers/igdb/co1wyy_2x.jpg").read_bytes() == jpeg("t_cover_big_2x/co1wyy")
    assert list((data_dir / DIRECTORY).iterdir()) != []
    assert not list((data_dir / DIRECTORY).glob("*.partial"))


@respx.mock
async def test_a_second_run_downloads_nothing(
    db: Database, session: AsyncSession, seeded: None, client: IgdbClient, data_dir: Path
) -> None:
    cdn = Cdn({1942: "co1wyy"}).mount()
    await anchored(session, 1942)
    await session.commit()

    await run(db, client, data_dir)
    await run(db, client, data_dir)

    assert len(cdn.downloads) == 2


@respx.mock
async def test_a_file_gone_from_disk_is_fetched_again(
    db: Database, session: AsyncSession, seeded: None, client: IgdbClient, data_dir: Path
) -> None:
    cdn = Cdn({1942: "co1wyy"}).mount()
    await anchored(session, 1942)
    await session.commit()
    await run(db, client, data_dir)
    (data_dir / "covers/igdb/co1wyy_2x.jpg").unlink()

    await run(db, client, data_dir)

    assert len(cdn.downloads) == 4
    assert (data_dir / "covers/igdb/co1wyy_2x.jpg").is_file()


@respx.mock
async def test_a_changed_cover_gets_new_rows_and_the_old_files_go(
    db: Database,
    session: AsyncSession,
    seeded: None,
    client: IgdbClient,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Cdn({1942: "co1wyy"}).mount()
    work = await anchored(session, 1942)
    await session.commit()
    await run(db, client, data_dir)
    first = {image_id for (image_id,) in await session.execute(select(ImageAsset.id))}

    # IGDB answers anew once the cached answer is old.
    import ludarium.covers as covers_module

    monkeypatch.setattr(covers_module, "MAX_AGE", covers_module.timedelta(0))
    Cdn({1942: "co9new"}).mount()
    await run(db, client, data_dir)

    assert [path for _, path in await covers_of(db, work.id)] == [
        "covers/igdb/co9new.jpg",
        "covers/igdb/co9new_2x.jpg",
    ]
    async with db.session_factory() as reader:
        now = set(await reader.scalars(select(ImageAsset.id)))
    assert now.isdisjoint(first)
    assert sorted(path.name for path in (data_dir / DIRECTORY).iterdir()) == [
        "co9new.jpg",
        "co9new_2x.jpg",
    ]


@respx.mock
async def test_a_cover_missing_at_one_size_is_skipped_at_both(
    db: Database, session: AsyncSession, seeded: None, client: IgdbClient, data_dir: Path
) -> None:
    Cdn({1942: "co1wyy"}, missing=frozenset({"t_cover_big_2x/co1wyy"})).mount()
    work = await anchored(session, 1942)
    await session.commit()

    assert await run(db, client, data_dir) is SyncStatus.SUCCESS

    assert await covers_of(db, work.id) == []
    assert not any((data_dir / DIRECTORY).iterdir())


@respx.mock
async def test_an_unmatched_stub_is_not_asked_about(
    db: Database, session: AsyncSession, seeded: None, client: IgdbClient, data_dir: Path
) -> None:
    cdn = Cdn({}).mount()
    await make_work(session, "A stub")
    await session.commit()

    await run(db, client, data_dir)

    assert cdn.downloads == []
    assert not respx.calls or all(call.request.url != COVERS_URL for call in respx.calls)


@respx.mock
async def test_a_file_nobody_points_at_is_swept_and_nothing_outside_the_directory(
    db: Database, session: AsyncSession, seeded: None, client: IgdbClient, data_dir: Path
) -> None:
    Cdn({}).mount()
    (data_dir / DIRECTORY).mkdir(parents=True)
    (data_dir / DIRECTORY / "co0old.jpg").write_bytes(jpeg("old"))
    (data_dir / DIRECTORY / "co0cut.jpg.partial").write_bytes(b"half")
    (data_dir / "notes.txt").write_bytes(b"not ours to touch")

    await run(db, client, data_dir)

    assert list((data_dir / DIRECTORY).iterdir()) == []
    assert (data_dir / "notes.txt").exists()
