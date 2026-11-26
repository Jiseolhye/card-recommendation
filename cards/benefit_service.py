"""Read card data and estimate the discount for a single proposed payment."""

from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import transaction
from django.db.models import Q, Sum

from .benefit_calculation import (
    DiscountReason,
    DiscountTerms,
    HistoricalPayment,
    MonthBounds,
    calculate_discount,
    estimate_monthly_discount_used,
    month_bounds,
    remaining_monthly_limit,
)
from .models import BenefitType, CardBenefit, Category, Transaction, UserCard


@dataclass(frozen=True)
class CardDiscountEstimate:
    """Whole-won estimate and spending totals for the selected user card.

    Monthly usage is projected from saved transactions. Usage and remaining
    limit are None without a matching, capped benefit. Both precede the payment.
    """

    estimated_discount: int
    current_month_spending: int
    previous_month_spending: int
    estimated_monthly_discount_used: int | None
    remaining_monthly_limit: int | None
    reason: DiscountReason


@dataclass(frozen=True)
class BenefitUsage:
    benefit: CardBenefit
    estimated_used: int | None
    remaining: int | None


@dataclass(frozen=True)
class OwnedCardSummary:
    user_card: UserCard
    previous_month_spending: int
    current_month_spending: int
    benefits: tuple[BenefitUsage, ...]


def validate_payment_inputs(*, amount: int, category: str, as_of: datetime) -> MonthBounds:
    """Validate the proposed payment and return its Seoul month bounds in UTC."""
    if type(amount) is not int or amount <= 0:
        raise ValueError("결제금액은 양의 정수여야 합니다.")
    if category not in Category.values:
        raise ValueError("유효한 결제 업종을 선택하세요.")
    return month_bounds(as_of)


def _history(transactions, *, start, as_of):
    return transactions.filter(transaction_date__gte=start, transaction_date__lt=as_of.astimezone(UTC))


def _card_months(user_card, bounds, as_of):
    history = _history(user_card.transactions.all(), start=bounds.previous_start, as_of=as_of)
    spending = history.aggregate(
        previous=Sum("amount", filter=Q(transaction_date__lt=bounds.current_start), default=0),
        current=Sum("amount", filter=Q(transaction_date__gte=bounds.current_start), default=0),
    )
    return history, spending


def _discount_terms(benefit):
    return DiscountTerms(
        category=benefit.category,
        benefit_rate=benefit.benefit_rate,
        minimum_amount=benefit.minimum_amount,
        required_spending=benefit.required_spending,
        monthly_limit=benefit.monthly_limit,
        per_transaction_limit=benefit.per_transaction_limit,
    )


def _monthly_usage(*, terms, history, bounds, previous_spending):
    if terms.monthly_limit is None:
        return None, None

    rows = history.filter(
        transaction_date__gte=bounds.current_start, category=terms.category,
    ).values_list("transaction_date", "id", "amount", "category")
    used = estimate_monthly_discount_used(
        terms=terms,
        previous_month_spending=previous_spending,
        payments=(HistoricalPayment(*row) for row in rows),
    )
    return used, remaining_monthly_limit(terms.monthly_limit, used or 0)


def current_user_spending(*, user_id: int, as_of: datetime) -> int:
    """Sum the user's saved principal from the Seoul month start up to as_of."""
    bounds = month_bounds(as_of)
    transactions = Transaction.objects.filter(user_card__user_id=user_id)
    history = _history(transactions, start=bounds.current_start, as_of=as_of)
    return history.aggregate(total=Sum("amount", default=0))["total"]


@transaction.atomic
def read_owned_card_summary(*, user_id: int, user_card_id: int, as_of: datetime) -> OwnedCardSummary:
    """Read spending and per-category cap estimates for one owned card."""
    bounds = month_bounds(as_of)
    user_card = UserCard.objects.select_related("card").get(pk=user_card_id, user_id=user_id)
    history, spending = _card_months(user_card, bounds, as_of)
    benefits = user_card.card.benefits.filter(benefit_type=BenefitType.DISCOUNT).order_by("category")
    usage = []
    for benefit in benefits:
        used, remaining = _monthly_usage(
            terms=_discount_terms(benefit), history=history, bounds=bounds,
            previous_spending=spending["previous"],
        )
        usage.append(BenefitUsage(benefit, used, remaining))
    return OwnedCardSummary(user_card, spending["previous"], spending["current"], tuple(usage))


@transaction.atomic
def estimate_card_discount(
    *,
    user_id: int,
    user_card_id: int,
    amount: int,
    category: str,
    as_of: datetime,
) -> CardDiscountEstimate:
    """Estimate a positive KRW payment on an owned card at an aware datetime.

    Invalid payment inputs raise ValueError. Both a missing card and one owned
    by another user raise UserCard.DoesNotExist.
    """
    bounds = validate_payment_inputs(amount=amount, category=category, as_of=as_of)
    user_card = UserCard.objects.get(pk=user_card_id, user_id=user_id)

    history, spending = _card_months(user_card, bounds, as_of)
    benefit = CardBenefit.objects.filter(
        card_id=user_card.card_id, category=category, benefit_type=BenefitType.DISCOUNT,
    ).first()

    terms = None
    used = None
    remaining = None
    if benefit is not None:
        terms = _discount_terms(benefit)
        used, remaining = _monthly_usage(
            terms=terms, history=history, bounds=bounds, previous_spending=spending["previous"],
        )

    discount = calculate_discount(
        amount=amount,
        category=category,
        terms=terms,
        previous_month_spending=spending["previous"],
        estimated_monthly_used=used or 0,
    )
    return CardDiscountEstimate(
        estimated_discount=discount.amount,
        current_month_spending=spending["current"],
        previous_month_spending=spending["previous"],
        estimated_monthly_discount_used=used,
        remaining_monthly_limit=remaining,
        reason=discount.reason,
    )
