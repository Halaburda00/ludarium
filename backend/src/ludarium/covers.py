"""Cover art for every work IGDB has matched, kept on disk and served from here (#51).

Served rather than hotlinked, so the library does not go blank when IGDB's CDN
is down and does not tell IGDB what the user is browsing. On disk rather than in
the database, because the database is the one file every backup copies whole
(ADR-0019). Under the data directory, which git and the image build both
ignore: IGDB's images may not be redistributed.

Two sizes of each, IGDB's own presets, so the grid can let the browser choose:
a phone's high-density screen takes the sharp one, a desktop monitor the one a
third the size (ADR-0024). The files are IGDB's JPEGs as sent — nothing here
decodes or resizes an image, which is what keeps an imaging library out of the
container.
"""

import asyncio
import hashlib
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import delete, select

from ludarium.db import Database
from ludarium.enrichment import EnrichmentRun, Step
from ludarium.enums import EntityType, ImageKind
from ludarium.models import ExternalId, ImageAsset, Provider
from ludarium.models.cache import Payload
from ludarium.models.types import utcnow
from ludarium.providers.base import MalformedResponseError, whole_number
from ludarium.providers.igdb import IGDB_IMAGES, IMAGE_ID, IgdbClient
from ludarium.queries import in_batches

logger = logging.getLogger(__name__)

NAMESPACE: Final = "igdb"
SOURCE: Final = "igdb"
RESOURCE: Final = "covers"
# Relative to the data directory, and the only directory the sweep touches.
DIRECTORY: Final = Path("covers") / SOURCE
# IGDB answers one cover per game; 500 is its row limit, so one row per id asked
# cannot be cut short.
BATCH: Final = 500
# A game's cover rarely changes. A month matches the other steps.
MAX_AGE: Final = timedelta(days=30)
# Downloads in flight at once. The CDN documents no limit; this is politeness,
# and enough that a first run over a library of hundreds takes seconds.
AT_ONCE: Final = 4


@dataclass(frozen=True, slots=True)
class Size:
    preset: str
    suffix: str
    # IGDB's `cover_big` presets crop to exactly these, whatever the original.
    width: int
    height: int


SIZES: Final = (
    Size("t_cover_big", "", 264, 374),
    Size("t_cover_big_2x", "_2x", 528, 748),
)


@dataclass(frozen=True, slots=True)
class Fetched:
    work_id: int
    image_id: str
    files: tuple[tuple[Size, str, str], ...]  # size, relative path, checksum


def fetch_covers(igdb: IgdbClient, data_dir: Path) -> Step:
    """The cover half of the `igdb` step, run after anchoring."""

    async def covers(games: Sequence[str]) -> dict[str, Payload]:
        rows = await igdb.query(
            RESOURCE, f"fields game,image_id; where game = ({','.join(games)}); limit {BATCH};"
        )
        found: dict[str, Payload] = {}
        for row in sorted(rows, key=lambda row: whole_number(row.get("id")) or 0):
            game, image_id = whole_number(row.get("game")), row.get("image_id")
            if game is None or not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
                raise MalformedResponseError(f"igdb returned a cover it cannot name: {row}")
            # The oldest where a game has two, so the answer does not flicker.
            found.setdefault(str(game), {"image_id": image_id})
        return found

    async def step(run: EnrichmentRun) -> None:
        games = await _anchored(run.database)
        answers = await run.fetch(
            RESOURCE,
            sorted({str(game) for game in games.values()}, key=int),
            fetch=covers,
            batch_size=BATCH,
            max_age=MAX_AGE,
        )
        wanted = {
            work_id: image_id
            for work_id, game in games.items()
            if (image_id := _image_id(answers.get(str(game)))) is not None
        }
        missing = await _missing(run.database, data_dir, wanted)
        fetched = await _download(igdb, data_dir, missing)
        await _record(run, fetched)
        swept = await _sweep(run.database, data_dir)
        logger.info("%d covers fetched, %d files no row points at removed", len(fetched), swept)

    return step


def _image_id(payload: Payload | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    image_id = payload.get("image_id")
    return image_id if isinstance(image_id, str) and IMAGE_ID.fullmatch(image_id) else None


async def _anchored(database: Database) -> dict[int, int]:
    """Every anchored work and its IGDB game, from the authoritative `external_id` row."""

    async with database.reading_session_factory() as session:
        rows = await session.execute(
            select(ExternalId.entity_id, ExternalId.value).where(
                ExternalId.entity_type == EntityType.WORK,
                ExternalId.namespace == NAMESPACE,
            )
        )
        return {work_id: int(game) for work_id, game in rows if game.isdigit()}


async def _missing(
    database: Database, data_dir: Path, wanted: Mapping[int, str]
) -> list[tuple[int, str]]:
    """The works whose cover is not on disk as IGDB names it now, at every size."""

    recorded: list[tuple[int, str]] = []
    async with database.reading_session_factory() as session:
        for batch in in_batches(list(wanted)):
            recorded += [
                (image.entity_id, image.local_path)
                for image in await session.scalars(
                    select(ImageAsset).where(
                        ImageAsset.entity_type == EntityType.WORK,
                        ImageAsset.entity_id.in_(batch),
                        ImageAsset.kind == ImageKind.COVER,
                        ImageAsset.source_ref == SOURCE,
                        ImageAsset.fetched_at.is_not(None),
                    )
                )
                if image.local_path
            ]

    # Off the event loop, as every other touch of the disk here: one stat per
    # file, and on a NAS the disk may be slow or across the network.
    def on_disk() -> set[str]:
        return {path for _, path in recorded if (data_dir / path).is_file()}

    present = await asyncio.to_thread(on_disk)
    held: dict[int, set[str]] = {}
    for work_id, path in recorded:
        if path in present:
            held.setdefault(work_id, set()).add(path)
    return [
        (work_id, image_id)
        for work_id, image_id in wanted.items()
        if held.get(work_id, set()) != {_relative(image_id, size) for size in SIZES}
    ]


def _relative(image_id: str, size: Size) -> str:
    return (DIRECTORY / f"{image_id}{size.suffix}.jpg").as_posix()


async def _download(
    igdb: IgdbClient, data_dir: Path, missing: Sequence[tuple[int, str]]
) -> list[Fetched]:
    """Every size of every missing cover, written to disk before any row names it.

    A cover IGDB no longer has at one size is skipped at all of them: a row
    pair missing its sharp half would give the grid a `srcset` that lies.
    """

    gate = asyncio.Semaphore(AT_ONCE)

    async def one(image_id: str) -> tuple[tuple[Size, str, str], ...] | None:
        files = []
        for size in SIZES:
            async with gate:
                body = await igdb.image(image_id, size.preset)
            if body is None:
                logger.info("igdb has no %s for cover %s", size.preset, image_id)
                return None
            relative = _relative(image_id, size)
            await asyncio.to_thread(_write, data_dir / relative, body)
            files.append((size, relative, hashlib.sha256(body).hexdigest()))
        return tuple(files)

    # Once per image, not once per work. IGDB gives editions and remasters the
    # same box art, and two downloads of one file at once would each rename
    # the other's `.partial` out from under it.
    images = sorted({image_id for _, image_id in missing})
    downloaded = dict(zip(images, await asyncio.gather(*map(one, images)), strict=True))
    return [
        Fetched(work_id, image_id, files)
        for work_id, image_id in missing
        if (files := downloaded[image_id]) is not None
    ]


def _write(path: Path, body: bytes) -> None:
    """Whole or not at all: a reader never sees half a file under the final name."""

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_bytes(body)
    os.replace(partial, path)


async def _record(run: EnrichmentRun, fetched: Sequence[Fetched]) -> None:
    """The new rows for each work, replacing its old IGDB cover rows, in one transaction.

    Replaced rather than updated: the served URL names the row, and a browser
    told to keep that URL for good must never be handed a different picture
    under it.
    """

    if not fetched:
        return
    moment = utcnow()
    async with run.database.writing_session_factory() as session:
        reporter = await session.get_one(Provider, run.provider_id)
        for cover in fetched:
            await session.execute(
                delete(ImageAsset).where(
                    ImageAsset.entity_type == EntityType.WORK,
                    ImageAsset.entity_id == cover.work_id,
                    ImageAsset.kind == ImageKind.COVER,
                    ImageAsset.source_ref == reporter.key,
                )
            )
            for size, relative, checksum in cover.files:
                session.add(
                    ImageAsset(
                        entity_type=EntityType.WORK,
                        entity_id=cover.work_id,
                        kind=ImageKind.COVER,
                        source_ref=reporter.key,
                        remote_url=f"{IGDB_IMAGES}/{size.preset}/{cover.image_id}.jpg",
                        local_path=relative,
                        checksum=checksum,
                        width=size.width,
                        height=size.height,
                        fetched_at=moment,
                    )
                )
        await session.commit()


async def _sweep(database: Database, data_dir: Path) -> int:
    """Delete the files in IGDB's cover directory that no row points at any more.

    A replaced cover's old files, a deleted work's, and a download a crash cut
    short. Only that directory, and only files: nothing a user put there.
    """

    directory = data_dir / DIRECTORY
    if not directory.is_dir():
        return 0
    async with database.reading_session_factory() as session:
        kept = set(
            await session.scalars(
                select(ImageAsset.local_path).where(
                    ImageAsset.source_ref == SOURCE, ImageAsset.local_path.is_not(None)
                )
            )
        )

    def sweep() -> int:
        removed = 0
        for path in directory.iterdir():
            if path.is_file() and (DIRECTORY / path.name).as_posix() not in kept:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    return await asyncio.to_thread(sweep)
