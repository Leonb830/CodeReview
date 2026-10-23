import asyncio
from decimal import Decimal

import pytest

from codereview.budgets import Budget, BudgetExceeded, DeadlineExceeded


async def test_concurrent_workers_share_one_two_dollar_allowance():
    budget = Budget(worker_limit=2, seconds=600)
    results = await asyncio.gather(
        *(budget.reserve("worker", Decimal("0.75")) for _ in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, str) for result in results) == 2
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
    assert budget.usage().worker_reserved == 1.5
    assert budget.usage().calls == 2


async def test_settlement_releases_unused_estimate_but_not_unknown_cost():
    budget = Budget(worker_limit=2, seconds=600)
    unknown = await budget.reserve("worker", 1)
    known = await budget.reserve("worker", 1)
    await budget.settle(unknown, None)
    await budget.settle(known, Decimal("0.25"))
    assert budget.usage().worker_cost == 0.25
    assert budget.usage().worker_reserved == 1
    with pytest.raises(BudgetExceeded):
        await budget.reserve("worker", Decimal("0.76"))
    await budget.reserve("worker", Decimal("0.75"))


async def test_expired_deadline_blocks_new_work_but_allows_accounting():
    now = [100.0]
    budget = Budget(worker_limit=2, seconds=10, clock=lambda: now[0])
    reservation = await budget.reserve("worker", 1)
    now[0] = 110.0
    assert budget.remaining_seconds() == 0
    with pytest.raises(DeadlineExceeded):
        await budget.reserve("worker", 0)
    await budget.settle(reservation, Decimal("0.2"))
    assert budget.usage().worker_cost == 0.2


async def test_decimal_costs_can_use_exact_allowance_without_float_drift():
    budget = Budget(worker_limit=Decimal("0.3"), seconds=600)
    for _ in range(3):
        reservation = await budget.reserve("worker", 0.1)
        await budget.settle(reservation, 0.1)
    assert budget.usage().worker_cost == 0.3
    with pytest.raises(BudgetExceeded):
        await budget.reserve("worker", Decimal("0.000001"))


async def test_billed_overrun_is_recorded_and_stops_subsequent_work():
    budget = Budget(worker_limit=2, seconds=600)
    reservation = await budget.reserve("worker", 1)
    await budget.settle(reservation, Decimal("2.1"))
    assert budget.usage().worker_cost == 2.1
    assert budget.usage().worker_reserved == 0
    with pytest.raises(BudgetExceeded):
        await budget.reserve("worker", 0)
    # Role accounting is separate: worker spend never drains coordinator money.
    coordinator = await budget.reserve("coordinator", 1)
    await budget.settle(coordinator, Decimal("0.5"))
    assert budget.usage().coordinator_cost == 0.5


async def test_reconciliation_is_idempotent_and_rejects_conflicting_bills():
    budget = Budget(worker_limit=2, seconds=600)
    reservation = await budget.reserve("worker", 1)
    await asyncio.gather(*(budget.settle(reservation, 0.2) for _ in range(4)))
    await budget.settle(reservation, None)
    assert budget.usage().worker_cost == 0.2
    assert budget.usage().worker_reserved == 0
    with pytest.raises(ValueError, match="Conflicting"):
        await budget.settle(reservation, 0.3)
    assert budget.usage().worker_cost == 0.2


async def test_fresh_coordinator_headroom_does_not_double_count_settled_cost():
    budget = Budget(worker_limit=2, seconds=600)
    await budget.set_coordinator_remaining(1)
    first = await budget.reserve("coordinator", 0.8)
    await budget.settle(first, 0.4)
    await budget.set_coordinator_remaining(0.6)
    outstanding = await budget.reserve("coordinator", 0.6)
    with pytest.raises(BudgetExceeded):
        await budget.reserve("coordinator", 0.01)
    await budget.settle(outstanding, None)
    await budget.set_coordinator_remaining(0)
    assert budget.usage().coordinator_reserved == 0.6
    with pytest.raises(BudgetExceeded):
        await budget.reserve("coordinator", 0.01)


async def test_remote_headroom_cannot_raise_configured_monthly_allowance():
    budget = Budget(worker_limit=2, seconds=600, coordinator_limit=5)
    await budget.set_coordinator_remaining(100)
    with pytest.raises(BudgetExceeded):
        await budget.reserve("coordinator", 6)


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf"), True])
async def test_invalid_costs_cannot_corrupt_the_ledger(amount):
    budget = Budget(worker_limit=2, seconds=600)
    reservation = await budget.reserve("worker", 1)
    with pytest.raises(ValueError):
        await budget.settle(reservation, amount)
    with pytest.raises(ValueError):
        await budget.reserve("worker", amount)
    with pytest.raises(ValueError):
        await budget.set_coordinator_remaining(amount)
    assert budget.usage().worker_reserved == 1
    assert budget.usage().calls == 1


async def test_usage_snapshot_cannot_modify_internal_accounting():
    budget = Budget(worker_limit=2, seconds=600)
    await budget.reserve("worker", 2)
    snapshot = budget.usage()
    snapshot.worker_reserved = 0
    with pytest.raises(BudgetExceeded):
        await budget.reserve("worker", 1)
