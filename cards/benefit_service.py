"""Read card data and estimate the discount for a single proposed payment."""

from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import transaction
from django.db.models import Q, Sum

from .benefit_calculation import (
    DiscountReason,
    DiscountTerms,
    HistoricalPayment,
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
    if type(amount) is not int or amount <= 0:
        raise ValueError("결제금액은 양의 정수여야 합니다.")
    if category not in Category.values:
        raise ValueError("유효한 결제 업종을 선택하세요.")
    bounds = month_bounds(as_of)
    user_card = UserCard.objects.get(pk=user_card_id, user_id=user_id)

    # Filter both months in UTC while keeping the Seoul month boundaries.
    history = Transaction.objects.filter(
        user_card_id=user_card.pk,
        transaction_date__gte=bounds.previous_start,
        transaction_date__lt=as_of.astimezone(UTC),
    )
    spending = history.aggregate(
        previous=Sum("amount", filter=Q(transaction_date__lt=bounds.current_start), default=0),
        current=Sum("amount", filter=Q(transaction_date__gte=bounds.current_start), default=0),
    )
    benefit = CardBenefit.objects.filter(
        card_id=user_card.card_id, category=category, benefit_type=BenefitType.DISCOUNT,
    ).first()

    terms = None
    used = None
    remaining = None
    if benefit is not None:
        terms = DiscountTerms(
            category=benefit.category,
            benefit_rate=benefit.benefit_rate,
            minimum_amount=benefit.minimum_amount,
            required_spending=benefit.required_spending,
            monthly_limit=benefit.monthly_limit,
            per_transaction_limit=benefit.per_transaction_limit,
        )
        if terms.monthly_limit is not None:
            rows = history.filter(
                transaction_date__gte=bounds.current_start, category=terms.category,
            ).values_list("transaction_date", "id", "amount", "category")
            used = estimate_monthly_discount_used(
                terms=terms,
                previous_month_spending=spending["previous"],
                payments=(HistoricalPayment(*row) for row in rows),
            )
            remaining = remaining_monthly_limit(terms.monthly_limit, used or 0)

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
