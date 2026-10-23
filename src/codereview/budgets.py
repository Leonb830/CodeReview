"""Atomic run-level admission and conservative accounting for model calls.

This ledger complements the provider's monthly key cap. It cannot make that
remote cap atomic across separate machines, or prove that cancelling an HTTP
request stopped upstream billing. Unknown charges therefore retain reservations.
"""

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import uuid4

from codereview.models import Usage

Role = Literal["coordinator", "worker"]
Money = Decimal | float | int


class BudgetExceeded(RuntimeError):
    """The requested cost reservation would exceed its role's allowance."""


class DeadlineExceeded(RuntimeError):
    """The operation's end-to-end monotonic deadline has elapsed."""


def _money(value: Money) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Money must be a finite, nonnegative number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Money must be a finite, nonnegative number") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("Money must be a finite, nonnegative number")
    return amount


@dataclass
class _Reservation:
    role: Role
    estimate: Decimal
    actual: Decimal | None = None


class Budget:
    """One run's worker allowance, coordinator headroom, and wall-time budget.

    Admission and settlement are serialized within the event loop. ``usage``
    returns a detached snapshot; monetary arithmetic remains Decimal internally.
    Each successful reservation is one attempted model call, including retries.
    """

    def __init__(
        self,
        worker_limit: Money,
        seconds: float,
        coordinator_limit: Money = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Budget duration must be finite and positive")
        self._worker_limit = _money(worker_limit)
        self._coordinator_limit = _money(coordinator_limit)
        self._coordinator_remaining = self._coordinator_limit
        self._clock = clock
        self._deadline = clock() + seconds
        self._lock = asyncio.Lock()
        self._reservations: dict[str, _Reservation] = {}
        self._spent: dict[Role, Decimal] = {
            "coordinator": Decimal(0),
            "worker": Decimal(0),
        }
        self._reserved: dict[Role, Decimal] = {
            "coordinator": Decimal(0),
            "worker": Decimal(0),
        }

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    async def reserve(self, role: Role, amount: Money) -> str:
        """Reserve before dispatch; competing calls cannot spend the same money."""
        if role not in self._spent:
            raise ValueError("Unknown budget role")
        estimate = _money(amount)
        async with self._lock:
            if self.remaining_seconds() <= 0:
                raise DeadlineExceeded("Operation deadline reached; no new model work admitted")
            limit = self._worker_limit if role == "worker" else self._coordinator_remaining
            if self._spent[role] + self._reserved[role] + estimate > limit:
                raise BudgetExceeded(f"{role} allowance cannot cover this model request")
            reservation_id = uuid4().hex
            self._reservations[reservation_id] = _Reservation(role, estimate)
            self._reserved[role] += estimate
            return reservation_id

    async def settle(self, reservation_id: str, actual_cost: Money | None) -> None:
        """Record billed cost even after expiry or a higher-than-estimated bill.

        None means unknown, never free. An identical settlement is idempotent;
        a conflicting second settlement is rejected rather than corrupting totals.
        """
        actual = None if actual_cost is None else _money(actual_cost)
        async with self._lock:
            reservation = self._reservations[reservation_id]
            if actual is None:
                return
            if reservation.actual is not None:
                if reservation.actual != actual:
                    raise ValueError("Conflicting cost for an already settled reservation")
                return
            self._reserved[reservation.role] -= reservation.estimate
            self._spent[reservation.role] += actual
            reservation.actual = actual

    async def set_coordinator_remaining(self, remaining: Money) -> None:
        """Apply fresh provider headroom without counting this run's spend twice.

        The provider value already excludes charges it has recorded, potentially
        including this run. Outstanding reservations still reduce local headroom;
        that conservative overlap is preferable to releasing unknown costs. This
        snapshot cannot coordinate other machines; the shared key enforces their
        common monthly cap.
        """
        available = _money(remaining)
        async with self._lock:
            self._coordinator_remaining = min(
                self._coordinator_limit,
                self._spent["coordinator"] + available,
            )

    def usage(self) -> Usage:
        """Return an accounting snapshot, not mutable access to the ledger."""
        return Usage(
            worker_cost=float(self._spent["worker"]),
            coordinator_cost=float(self._spent["coordinator"]),
            worker_reserved=float(self._reserved["worker"]),
            coordinator_reserved=float(self._reserved["coordinator"]),
            calls=len(self._reservations),
        )
