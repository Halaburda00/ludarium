import logging
from dataclasses import asdict, dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import titles
from ludarium.enums import LicenceClass, ProviderKind, SourceKind
from ludarium.models import Provider, Work

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    """The code-owned half of a provider row.

    Runtime columns — `enabled`, `status`, `last_success_at`, `last_error` —
    are deliberately absent: seeding must not re-enable a provider the user
    switched off, nor erase the health of the last run (rule 4).
    """

    key: str
    kind: ProviderKind
    source_kind: SourceKind
    display_name: str
    licence_class: LicenceClass = LicenceClass.REDISTRIBUTABLE
    store_url_template: str | None = None
    attribution_html: str | None = None
    precedence_weight: int = 100


PROVIDER_SEED: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        key="steam",
        kind=ProviderKind.PLATFORM,
        source_kind=SourceKind.PLATFORM_API,
        display_name="Steam",
        # We never launch a game, so the store page is the answer to
        # "where do I find this".
        store_url_template="https://store.steampowered.com/app/{id}",
    ),
    ProviderSpec(
        key="manual",
        kind=ProviderKind.MANUAL,
        source_kind=SourceKind.MANUAL,
        display_name="Manual entry",
    ),
)


async def seed_providers(session: AsyncSession) -> None:
    """Bring the provider table in step with the code. Safe to run on every start.

    Reads the table, decides, and then writes — with nothing between the two
    but the transaction, which is the whole of the answer (ADR-0017). Startup
    takes a writing session, so on SQLite a second instance blocks at `BEGIN
    IMMEDIATE` until the first has committed and then reads the rows it wrote:
    measured, the loser's `SELECT` returned at +0.383 s against the winner's
    +0.017 s, and saw `steam` already there.

    On an engine that lets both read the table as empty the unique constraint
    on `provider.key` is what catches them, and the loser fails to start rather
    than duplicating a provider. That is the intended outcome: seeding is the
    first thing an instance does, so there is nothing yet to lose by refusing.
    """

    existing = {provider.key: provider for provider in await session.scalars(select(Provider))}

    for spec in PROVIDER_SEED:
        columns = asdict(spec)
        provider = existing.get(spec.key)
        if provider is None:
            session.add(Provider(**columns))
            continue
        for column, value in columns.items():
            if getattr(provider, column) != value:
                setattr(provider, column, value)

    await session.commit()


async def reconcile_sort_keys(session: AsyncSession) -> int:
    """Rewrite every stored `sort_key` the running code would not compute. Safe on every start.

    The keyset under `GET /api/works` is correct only while each stored key is
    what `titles.sort_key` returns now, and three things can make it otherwise
    without an error anywhere. A newer Python brings a newer Unicode database,
    which folds characters the older one did not know — measured between 3.13
    and 3.14, 95 code points, every one unassigned in the older version, so a
    title is affected only if it holds a character that did not exist when its
    key was computed. A bulk `UPDATE` bypasses the model's validator. And a
    change to the fold can ship without the migration that recomputes the
    column.

    A stored Unicode version would notice the first of those and nothing else.
    Checking every row notices all three, and costs 64 ms at 20 000 works —
    cheaper than the bookkeeping, which is the difference from embeddings,
    where recomputing is what takes the minutes (ADR-0018).

    Logged when anything was rewritten: a key that needed repair is a fact about
    how the instance was changed, and healing it silently would hide that.
    """

    rows = await session.execute(select(Work.id, Work.sort_title, Work.sort_key))
    stale = [
        {"id": work_id, "sort_key": fresh}
        for work_id, sort_title, stored in rows
        if (fresh := titles.sort_key(sort_title)) != stored
    ]
    if stale:
        await session.execute(update(Work), stale)
        logger.warning(
            "sort keys rewritten because the running code computes them differently: %d",
            len(stale),
        )
    await session.commit()
    return len(stale)
