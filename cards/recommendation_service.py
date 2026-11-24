"""Compare the estimated discounts of a user's owned cards."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.db import transaction

from .benefit_service import CardDiscountEstimate, estimate_card_discount, validate_payment_inputs
from .models import UserCard


class RecommendationReason(StrEnum):
    HIGHEST_ESTIMATED_DISCOUNT = "highest_estimated_discount"
    TIED_HIGHEST_ESTIMATED_DISCOUNT = "tied_highest_estimated_discount"
    NO_POSITIVE_DISCOUNT = "no_positive_discount"
    NO_CARDS = "no_cards"


@dataclass(frozen=True)
class CardComparison:
    """Identify a user's owned card and carry its full Week 5 discount estimate."""

    user_card_id: int
    card_id: int
    card_name: str
    issuer: str
    estimate: CardDiscountEstimate


@dataclass(frozen=True)
class CardRecommendation:
    """The selected card and its estimated discount, ranked against all owned cards.

    The selected card is None and its discount is zero without a recommendation.
    Additional savings is the difference from the second ranked card, or None
    when there is no recommendation or no second card. A tie refers to multiple
    cards sharing the highest positive estimate. Actual discounts can differ.
    """

    recommended_card: CardComparison | None
    estimated_discount: int
    reason: RecommendationReason
    comparisons: tuple[CardComparison, ...]
    additional_savings: int | None
    tied_for_first: bool


def rank_card_comparisons(comparisons: Iterable[CardComparison]) -> CardRecommendation:
    """Rank existing estimates without database access or mutating the input."""
    # Prefer larger discounts. The ID gives equal discounts a stable order.
    ranked = tuple(sorted(comparisons, key=lambda card: (-card.estimate.estimated_discount, card.user_card_id)))
    if not ranked or ranked[0].estimate.estimated_discount <= 0:
        return CardRecommendation(
            recommended_card=None,
            estimated_discount=0,
            reason=RecommendationReason.NO_POSITIVE_DISCOUNT if ranked else RecommendationReason.NO_CARDS,
            comparisons=ranked,
            additional_savings=None,
            tied_for_first=False,
        )

    best = ranked[0]
    best_discount = best.estimate.estimated_discount
    additional_savings = None
    if len(ranked) > 1:
        additional_savings = best_discount - ranked[1].estimate.estimated_discount
    tied_for_first = additional_savings == 0
    reason = (
        RecommendationReason.TIED_HIGHEST_ESTIMATED_DISCOUNT
        if tied_for_first else RecommendationReason.HIGHEST_ESTIMATED_DISCOUNT
    )
    return CardRecommendation(
        recommended_card=best,
        estimated_discount=best_discount,
        reason=reason,
        comparisons=ranked,
        additional_savings=additional_savings,
        tied_for_first=tied_for_first,
    )


@transaction.atomic
def recommend_best_card(
    *,
    user_id: int,
    amount: int,
    category: str,
    as_of: datetime,
) -> CardRecommendation:
    """Read the user's cards and compare estimates for the same proposed payment.

    The caller must supply the authenticated user's ID. A future Django view
    should use request.user.pk. Invalid payment input raises ValueError.
    Ownership and database errors from the individual estimates propagate.
    """
    validate_payment_inputs(amount=amount, category=category, as_of=as_of)
    owned_cards = UserCard.objects.filter(user_id=user_id).select_related("card")

    comparisons = []
    for owned_card in owned_cards:
        estimate = estimate_card_discount(
            user_id=user_id,
            user_card_id=owned_card.pk,
            amount=amount,
            category=category,
            as_of=as_of,
        )
        comparisons.append(CardComparison(
            user_card_id=owned_card.pk,
            card_id=owned_card.card_id,
            card_name=owned_card.card.name,
            issuer=owned_card.card.issuer,
            estimate=estimate,
        ))
    return rank_card_comparisons(comparisons)
