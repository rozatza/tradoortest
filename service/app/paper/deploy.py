"""Deploy and undeploy one pinned strategy version on the paper book.

Deploy copies that version's canonical IR into the registration row and
sets ``registered_at`` from the bar-store clock. A later save is a new
version. It does not replace the document the broker reads.

Undeploy removes the registration, so the scheduler and the broker stop
evaluating that version. An open position is left as stored. Its state is
``open_unmanaged``: qty, entry, basis, and the OCO levels stay, and no
later bar moves them until a new deploy. A resting order in status
``open`` is marked ``canceled``. No fill is written for that cancel and
none is written to flatten the position. ``ORDER_CANCELED`` and
``STRATEGY_UNDEPLOYED`` are appended. The wallet, the position rows, the
paper cursor, and existing fills are not updated.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ir.canonical import canonical_json
from app.live.scheduler import require_paper_strategy
from app.ohlcv.errors import RegistrationRefused, StoreError
from app.ohlcv.store import SqliteBarStore
from app.paper.errors import (
    PaperConflict,
    PaperLimitReached,
    PaperNotDeployable,
    PaperNotDeployed,
)
from app.strategies.store import StrategyStore


@dataclass(frozen=True, slots=True)
class Deployment:
    """One running paper version. ``created`` is false when it was already running."""

    strategy_id: str
    version: int
    ir_hash: str
    registered_at: int
    created: bool


@dataclass(frozen=True, slots=True)
class Undeployment:
    """The book after evaluation stopped.

    ``position_state`` is ``open_unmanaged`` or ``flat``. ``orders_canceled``
    counts resting orders marked ``canceled`` by this call.
    """

    strategy_id: str
    version: int
    orders_canceled: int
    position_state: str


def deploy_version(
    strategies: StrategyStore,
    bars: SqliteBarStore,
    user_id: str,
    strategy_id: str,
    version: int,
    *,
    limit: int,
) -> Deployment:
    """Pin ``version`` and register it. The body of the request is not an IR."""
    record = strategies.get_version(user_id, strategy_id, version)
    try:
        prepared = require_paper_strategy(strategy_id, version, record.ir)
    except ValueError:
        raise PaperNotDeployable() from None
    try:
        registered_at, created = bars.save_live_registration(
            user_id=user_id,
            strategy_id=strategy_id,
            version=version,
            document_json=canonical_json(prepared.document),
            bar_size=prepared.bar_size,
            limit=limit,
        )
    except RegistrationRefused as exc:
        if exc.code == "limit":
            raise PaperLimitReached() from None
        raise
    except StoreError:
        raise PaperConflict() from None
    return Deployment(
        strategy_id=strategy_id,
        version=version,
        ir_hash=record.ir_hash,
        registered_at=registered_at,
        created=created,
    )


def undeploy_version(
    bars: SqliteBarStore,
    user_id: str,
    strategy_id: str,
    version: int,
) -> Undeployment:
    """Stop evaluation. Leave an open position. Cancel a resting order. No fill."""
    result = bars.undeploy_paper(
        user_id=user_id, strategy_id=strategy_id, version=version
    )
    if result is None:
        raise PaperNotDeployed()
    orders_canceled, position_state = result
    return Undeployment(
        strategy_id=strategy_id,
        version=version,
        orders_canceled=orders_canceled,
        position_state=position_state,
    )
