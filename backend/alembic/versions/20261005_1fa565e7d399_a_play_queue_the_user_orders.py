"""a play queue the user orders

Revision ID: 1fa565e7d399
Revises: f878bd6caf3a
Create Date: 2026-10-05 19:02:00.467474

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1fa565e7d399"
down_revision: str | Sequence[str] | None = "f878bd6caf3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from `PlayStatus`, so a later value cannot reach
# back and change what this revision created.
BEFORE = ("not_started", "playing", "completed", "mastered", "dropped", "on_hold", "wishlist")
AFTER = (
    "not_started",
    "queued",
    "playing",
    "completed",
    "mastered",
    "dropped",
    "on_hold",
    "wishlist",
)
CHECK = "play_status"
QUEUED = (
    "(play_status = 'queued' AND queue_position IS NOT NULL AND queue_position >= 1)"
    " OR (play_status <> 'queued' AND queue_position IS NULL)"
)


def _within(values: tuple[str, ...]) -> str:
    return f"play_status IN ({', '.join(f"'{value}'" for value in values)})"


def upgrade() -> None:
    with op.batch_alter_table("user_work_state") as batch:
        batch.add_column(sa.Column("queue_position", sa.Integer(), nullable=True))
        batch.drop_constraint(op.f(f"ck_user_work_state_{CHECK}"), type_="check")
        batch.create_check_constraint(CHECK, _within(AFTER))
        batch.create_check_constraint("queue_position_iff_queued", QUEUED)
        batch.create_unique_constraint(
            op.f("uq_user_work_state_user_id_queue_position"), ["user_id", "queue_position"]
        )


def downgrade() -> None:
    # Lossy by necessity: the older schema has no queue. A queued work goes back
    # to the status it is a refinement of.
    op.execute(
        "UPDATE user_work_state SET play_status = 'not_started', queue_position = NULL"
        " WHERE play_status = 'queued'"
    )
    with op.batch_alter_table("user_work_state") as batch:
        batch.drop_constraint(op.f("uq_user_work_state_user_id_queue_position"), type_="unique")
        batch.drop_constraint(op.f("ck_user_work_state_queue_position_iff_queued"), type_="check")
        batch.drop_constraint(op.f(f"ck_user_work_state_{CHECK}"), type_="check")
        batch.create_check_constraint(CHECK, _within(BEFORE))
        batch.drop_column("queue_position")
