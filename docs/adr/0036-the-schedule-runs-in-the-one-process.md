# ADR-0036 The schedule runs in the one process

Status: accepted, 2026-10-07.

## Context

M4 is done when the library "refreshes itself without being asked" (#127).
Until now a sync ran only when someone pressed the button. `SyncTrigger.scheduled`
and the `apscheduler` dependency had been waiting for this since M1.

## Decision

**APScheduler's `AsyncIOScheduler`, inside the application process.** It
starts in `lifespan` after the database is ready and stops before the HTTP
client and the engine are closed. There is no separate worker or queue: a
self-hosted catalogue syncs a handful of accounts a few times a day, and a
second process to deploy and watch would cost more than everything it runs.

**One job per platform with a library client, doing what the sync button
does.** Every active, connected account of the platform is synced in its own
run with `trigger = scheduled`, each in its own session. The enrichment steps
that follow a sync (`steps.FOLLOWS`) run after it, as they do after a manual
sync. One job per platform keeps them apart (rule 4): a slow Epic does not
delay Steam.

**An account already syncing is skipped, not queued.** The partial unique
index on open runs already refuses a second run, and `SyncInProgressError`
means someone pressed the button or the last scheduled run is still going.
Either way the account is being synced. `max_instances = 1` and `coalesce`
keep a slow job from overlapping itself, and keep a sleeping laptop from
firing every run it missed on waking.

**The first run is an interval after the platform's last success.** Counting
from the start would sync everything again on every restart. A platform that
is overdue, or has never synced, runs soon, one minute after the one before
it, so a restart never asks every platform at once.

**Configured by environment, in hours.** `LUDARIUM_SYNC_INTERVAL_HOURS`
(default 6) applies to every platform, and `LUDARIUM_SYNC_INTERVALS`, a JSON
object, overrides it per platform. `0` turns a platform off. An entry for
something that cannot sync stops the start rather than being ignored. There
is no UI for it yet.

**Stopping waits for runs to close.** APScheduler cancels the tasks of a
coroutine job and returns without waiting. The engine would then be disposed
under a run still closing itself, and its row would stay `running` until
`ORPHAN_AFTER` reclaims it. The schedule keeps its own set of running jobs,
cancels them and waits, so a cancelled run is recorded `failed` at once.

**Not in a demo.** A demo has nothing to sync and may write nothing (ADR-0034).

## Consequences

- **One process.** Two uvicorn workers would start two schedulers. The index
  still stops two runs on one account, but every request to the platform would
  be made twice and spend its rate limit twice. The documented command and the
  Docker image run one process, and should keep doing so.
- A scheduled run renews Epic's refresh token exactly as a manual one does
  (`renewed_secret`), including when the run fails.
- A run that fails leaves the account's and the provider's status red, as a
  manual run's does. Nothing retries before the next interval.
