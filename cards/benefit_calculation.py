"""Calculate expected discounts without accessing the database."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import NamedTuple
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


class DiscountReason(StrEnum):
    APPLIED = "applied"
    NO_MATCHING_BENEFIT = "no_matching_benefit"
    PREVIOUS_SPENDING_NOT_MET = "previous_spending_not_met"
    MINIMUM_AMOUNT_NOT_MET = "minimum_amount_not_met"
    MONTHLY_LIMIT_EXHAUSTED = "monthly_limit_exhausted"
    PER_TRANSACTION_LIMIT_ZERO = "per_transaction_limit_zero"
    ROUNDED_TO_ZERO = "rounded_to_zero"


@dataclass(frozen=True)
class MonthBounds:
    previous_start: datetime
    current_start: datetime
    next_start: datetime


@dataclass(frozen=True)
class DiscountTerms:
    category: str
    benefit_rate: Decimal
    minimum_amount: int
    required_spending: int
    monthly_limit: int | None
    per_transaction_limit: int | None


@dataclass(frozen=True)
class Discount:
    amount: int
    reason: DiscountReason


class HistoricalPayment(NamedTuple):
    transaction_date: datetime
    id: int
    amount: int
    category: str


def month_bounds(as_of: datetime) -> MonthBounds:
    """Return the Seoul calendar month boundaries as UTC datetimes."""
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise ValueError("기준 시각은 시간대가 포함된 datetime이어야 합니다.")

    current = as_of.astimezone(SEOUL).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    previous = (current - timedelta(days=1)).replace(day=1)
    # Moving 32 days from the first always enters the following month.
    following = (current + timedelta(days=32)).replace(day=1)
    return MonthBounds(previous.astimezone(UTC), current.astimezone(UTC), following.astimezone(UTC))


def remaining_monthly_limit(monthly_limit: int | None, used: int) -> int | None:
    """Return the remaining won, or None when the monthly limit is unlimited."""
    if monthly_limit is None:
        return None
    return max(0, monthly_limit - used)


def calculate_discount(
    *,
    amount: int,
    category: str,
    terms: DiscountTerms | None,
    previous_month_spending: int,
    estimated_monthly_used: int = 0,
) -> Discount:
    """Calculate one discount in whole won and report the first applicable reason."""
    if terms is None or category != terms.category:
        return Discount(0, DiscountReason.NO_MATCHING_BENEFIT)
    if previous_month_spending < terms.required_spending:
        return Discount(0, DiscountReason.PREVIOUS_SPENDING_NOT_MET)
    if amount < terms.minimum_amount:
        return Discount(0, DiscountReason.MINIMUM_AMOUNT_NOT_MET)

    base = Decimal(amount) * terms.benefit_rate / Decimal(100)
    discount = int(base.to_integral_value(rounding=ROUND_DOWN))
    if terms.per_transaction_limit is not None:
        discount = min(discount, terms.per_transaction_limit)

    remaining = remaining_monthly_limit(terms.monthly_limit, estimated_monthly_used)
    if remaining is not None:
        discount = min(discount, remaining)

    if discount > 0:
        return Discount(discount, DiscountReason.APPLIED)
    if remaining == 0:
        return Discount(0, DiscountReason.MONTHLY_LIMIT_EXHAUSTED)
    if terms.per_transaction_limit == 0:
        return Discount(0, DiscountReason.PER_TRANSACTION_LIMIT_ZERO)
    return Discount(0, DiscountReason.ROUNDED_TO_ZERO)


def estimate_monthly_discount_used(
    *,
    terms: DiscountTerms,
    previous_month_spending: int,
    payments: Iterable[HistoricalPayment],
) -> int | None:
    """Replay one month's eligible history using current terms and prior-month spending."""
    if terms.monthly_limit is None:
        return None

    used = 0
    for payment in sorted(payments, key=lambda row: (row.transaction_date, row.id)):
        # Only the amount that fit both limits consumes the month's allowance.
        discount = calculate_discount(
            amount=payment.amount,
            category=payment.category,
            terms=terms,
            previous_month_spending=previous_month_spending,
            estimated_monthly_used=used,
        )
        used += discount.amount
        if used == terms.monthly_limit:
            break
    return used
