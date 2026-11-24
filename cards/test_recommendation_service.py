from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .benefit_calculation import DiscountReason
from .benefit_service import CardDiscountEstimate, estimate_card_discount
from .models import Card, CardBenefit, Category, Transaction, UserCard
from .recommendation_service import (
    CardComparison,
    CardRecommendation,
    RecommendationReason,
    rank_card_comparisons,
    recommend_best_card,
)


class CardRankingTests(SimpleTestCase):
    def test_three_way_tie_is_stable_without_reordering_the_input(self):
        def comparison(user_card_id, discount):
            return CardComparison(
                user_card_id=user_card_id,
                card_id=user_card_id,
                card_name=f"테스트카드 {user_card_id}",
                issuer="가상카드사",
                estimate=CardDiscountEstimate(discount, 0, 0, None, None, DiscountReason.APPLIED),
            )

        cards = [comparison(9, 3000), comparison(2, 2000), comparison(6, 3000), comparison(3, 3000)]
        original = cards.copy()
        result = rank_card_comparisons(cards)

        self.assertEqual(cards, original)
        self.assertEqual(result, rank_card_comparisons(reversed(cards)))
        self.assertEqual(
            result,
            CardRecommendation(
                recommended_card=cards[3],
                estimated_discount=3000,
                reason=RecommendationReason.TIED_HIGHEST_ESTIMATED_DISCOUNT,
                comparisons=(cards[3], cards[2], cards[0], cards[1]),
                additional_savings=0,
                tied_for_first=True,
            ),
        )


class RecommendationServiceTests(TestCase):
    as_of = datetime(2026, 11, 15, 3, tzinfo=UTC)
    this_month = datetime(2026, 11, 10, 3, tzinfo=UTC)
    previous_month = datetime(2026, 10, 15, 3, tzinfo=UTC)

    @classmethod
    def setUpTestData(cls):
        call_command("seed_mvp_cards", stdout=StringIO())
        User = get_user_model()
        cls.user = User.objects.create_user(username="recommendation-learner")
        cls.other_user = User.objects.create_user(username="other-learner")
        cls.cards = {suffix: Card.objects.get(name=f"테스트카드 {suffix}") for suffix in "ABCDE"}

    def own(self, suffix, *, user=None):
        return UserCard.objects.create(user=self.user if user is None else user, card=self.cards[suffix])

    def pay(self, user_card, amount, *, when=None, category=Category.FOOD):
        Transaction.objects.create(
            user_card=user_card,
            amount=amount,
            category=category,
            merchant_name="가상 가맹점",
            transaction_date=self.this_month if when is None else when,
        )

    def recommend(self, *, amount=50000, category=Category.FOOD, as_of=as_of):
        return recommend_best_card(user_id=self.user.pk, amount=amount, category=category, as_of=as_of)

    def assert_ranking(self, result, *expected):
        self.assertEqual(
            [(card.user_card_id, card.estimate.estimated_discount) for card in result.comparisons],
            [(card.pk, discount) for card, discount in expected],
        )

    def test_card_b_wins_and_returns_identity_and_savings(self):
        a, b = self.own("A"), self.own("B")
        result = self.recommend()
        self.assert_ranking(result, (b, 4000), (a, 2500))
        self.assertEqual(result.recommended_card, result.comparisons[0])
        self.assertEqual(
            result.recommended_card,
            CardComparison(
                b.pk, b.card_id, "테스트카드 B", "가상카드사 B",
                CardDiscountEstimate(4000, 0, 0, None, None, DiscountReason.APPLIED),
            ),
        )
        self.assertEqual(result.estimated_discount, 4000)
        self.assertEqual(result.reason, RecommendationReason.HIGHEST_ESTIMATED_DISCOUNT)
        self.assertEqual(result.additional_savings, 1500)
        self.assertFalse(result.tied_for_first)

    def test_card_c_rank_changes_at_previous_spending_threshold(self):
        a, c = self.own("A"), self.own("C")
        self.pay(c, 299999, when=self.previous_month)

        below = self.recommend()
        self.assert_ranking(below, (a, 2500), (c, 0))
        self.assertEqual(below.recommended_card.user_card_id, a.pk)
        self.assertEqual(below.comparisons[1].estimate.reason, DiscountReason.PREVIOUS_SPENDING_NOT_MET)
        self.assertEqual(below.additional_savings, 2500)

        self.pay(c, 1, when=self.previous_month)
        met = self.recommend()
        self.assert_ranking(met, (c, 5000), (a, 2500))
        self.assertEqual(met.recommended_card.user_card_id, c.pk)
        self.assertEqual(met.additional_savings, 2500)

    def test_card_d_rank_changes_at_minimum_amount(self):
        a, d = self.own("A"), self.own("D")
        below = self.recommend(amount=19999)
        self.assert_ranking(below, (a, 999), (d, 0))
        self.assertEqual(below.recommended_card.user_card_id, a.pk)
        self.assertEqual(below.comparisons[1].estimate.reason, DiscountReason.MINIMUM_AMOUNT_NOT_MET)

        met = self.recommend(amount=20000)
        self.assert_ranking(met, (d, 2400), (a, 1000))
        self.assertEqual(met.recommended_card.user_card_id, d.pk)
        self.assertEqual(met.estimated_discount, 2400)

    def test_card_e_rank_changes_as_monthly_limit_is_used_and_exhausted(self):
        a, e = self.own("A"), self.own("E")
        initial = self.recommend(amount=30000)
        self.assert_ranking(initial, (e, 3000), (a, 1500))
        self.assertEqual(initial.recommended_card.user_card_id, e.pk)
        self.assertEqual(initial.comparisons[0].estimate.estimated_monthly_discount_used, 0)

        for _ in range(3):
            self.pay(e, 50000)
        partial = self.recommend(amount=30000)
        self.assert_ranking(partial, (a, 1500), (e, 1000))
        self.assertEqual(partial.recommended_card.user_card_id, a.pk)
        self.assertEqual(partial.comparisons[1].estimate.estimated_monthly_discount_used, 9000)
        self.assertEqual(partial.additional_savings, 500)

        self.pay(e, 50000)
        exhausted = self.recommend(amount=30000)
        self.assert_ranking(exhausted, (a, 1500), (e, 0))
        self.assertEqual(exhausted.recommended_card.user_card_id, a.pk)
        self.assertEqual(exhausted.comparisons[1].estimate.reason, DiscountReason.MONTHLY_LIMIT_EXHAUSTED)
        self.assertEqual(exhausted.comparisons[1].estimate.estimated_monthly_discount_used, 10000)
        self.assertEqual(exhausted.additional_savings, 1500)

    def test_all_five_cards_rank_by_estimated_discount(self):
        a, b, c, d, e = [self.own(suffix) for suffix in "ABCDE"]
        self.pay(c, 299999, when=self.previous_month)
        result = self.recommend()
        self.assert_ranking(result, (d, 6000), (b, 4000), (e, 3000), (a, 2500), (c, 0))
        self.assertEqual(result.recommended_card.user_card_id, d.pk)
        self.assertEqual(result.additional_savings, 2000)

    def test_cards_a_and_e_tie_and_smaller_user_card_id_is_representative(self):
        e, a = self.own("E"), self.own("A")
        self.assertLess(e.pk, a.pk)
        result = self.recommend(amount=60000)
        self.assert_ranking(result, (e, 3000), (a, 3000))
        self.assertEqual(result.recommended_card.user_card_id, e.pk)
        self.assertEqual(result.reason, RecommendationReason.TIED_HIGHEST_ESTIMATED_DISCOUNT)
        self.assertTrue(result.tied_for_first)
        self.assertEqual(result.additional_savings, 0)

    def test_zero_discounts_keep_all_comparisons_without_recommendation(self):
        cards = [self.own(suffix) for suffix in "ABCDE"]
        result = self.recommend(category=Category.CAFE)
        self.assert_ranking(result, *((card, 0) for card in cards))
        self.assertEqual(
            result,
            CardRecommendation(None, 0, RecommendationReason.NO_POSITIVE_DISCOUNT, result.comparisons, None, False),
        )
        self.assertTrue(all(
            card.estimate.reason == DiscountReason.NO_MATCHING_BENEFIT for card in result.comparisons
        ))

    def test_no_owned_cards_returns_empty_comparisons(self):
        self.assertEqual(
            self.recommend(), CardRecommendation(None, 0, RecommendationReason.NO_CARDS, (), None, False),
        )

    def test_one_owned_card_has_no_comparison_savings(self):
        a = self.own("A")
        result = self.recommend()
        self.assert_ranking(result, (a, 2500))
        self.assertEqual(result.recommended_card.user_card_id, a.pk)
        self.assertIsNone(result.additional_savings)
        self.assertFalse(result.tied_for_first)

    def test_excludes_cards_owned_by_other_users(self):
        a = self.own("A")
        self.own("B", user=self.other_user)
        result = self.recommend()
        self.assert_ranking(result, (a, 2500))
        self.assertEqual(result.recommended_card.user_card_id, a.pk)

    def test_excludes_other_users_transactions_on_same_card_product(self):
        a, c = self.own("A"), self.own("C")
        other_c = self.own("C", user=self.other_user)
        self.pay(c, 299999, when=self.previous_month)
        self.pay(other_c, 300000, when=self.previous_month)
        self.pay(other_c, 50000)

        result = self.recommend()
        self.assert_ranking(result, (a, 2500), (c, 0))
        self.assertEqual(result.recommended_card.user_card_id, a.pk)
        estimate = result.comparisons[1].estimate
        self.assertEqual((estimate.previous_month_spending, estimate.current_month_spending), (299999, 0))
        self.assertEqual(estimate.reason, DiscountReason.PREVIOUS_SPENDING_NOT_MET)

    def test_invalid_inputs_raise_even_without_owned_cards(self):
        invalid_inputs = (
            *({"amount": amount} for amount in (0, -1, True, False, 50000.0, "50000")),
            {"category": "SHOPPING"},
            {"as_of": self.as_of.isoformat()},
            {"as_of": self.as_of.replace(tzinfo=None)},
        )
        for has_card in (False, True):
            if has_card:
                self.own("A")
            for inputs in invalid_inputs:
                with self.subTest(has_card=has_card, inputs=inputs), self.assertRaises(ValueError):
                    self.recommend(**inputs)

    def test_monthly_reset_at_seoul_midnight_changes_recommendation(self):
        a, e = self.own("A"), self.own("E")
        boundary = datetime(2026, 12, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        for _ in range(3):
            self.pay(e, 50000, when=boundary - timedelta(seconds=2))
        before = self.recommend(amount=30000, as_of=boundary - timedelta(seconds=1))
        after = self.recommend(amount=30000, as_of=boundary)
        self.assertEqual(before.recommended_card.user_card_id, a.pk)
        self.assertEqual(after.recommended_card.user_card_id, e.pk)
        self.assertEqual(after.estimated_discount, 3000)
        self.assertEqual(after.comparisons[0].estimate.estimated_monthly_discount_used, 0)

    def test_transactions_at_and_after_cutoff_do_not_affect_ranking(self):
        a, e = self.own("A"), self.own("E")
        for _ in range(2):
            self.pay(e, 50000)
        self.pay(e, 50000, when=self.as_of)
        self.pay(e, 50000, when=self.as_of + timedelta(seconds=2))
        exact = self.recommend(amount=30000)
        after = self.recommend(amount=30000, as_of=self.as_of + timedelta(seconds=1))
        self.assert_ranking(exact, (e, 3000), (a, 1500))
        self.assertEqual(exact.comparisons[0].estimate.estimated_monthly_discount_used, 6000)
        self.assert_ranking(after, (a, 1500), (e, 1000))
        self.assertEqual(after.comparisons[1].estimate.estimated_monthly_discount_used, 9000)

    def test_existing_estimator_keeps_validation_and_ownership_exceptions(self):
        a = self.own("A")
        inputs = {"user_id": self.user.pk, "user_card_id": a.pk,
                  "amount": 50000, "category": Category.FOOD, "as_of": self.as_of}
        for override in (
            {"amount": True},
            {"category": "SHOPPING"},
            {"as_of": self.as_of.replace(tzinfo=None)},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                estimate_card_discount(**(inputs | override))
        for override in ({"user_card_id": a.pk + 1}, {"user_id": self.other_user.pk}):
            with self.subTest(override=override), self.assertRaises(UserCard.DoesNotExist):
                estimate_card_discount(**(inputs | override))

    def test_calculation_failure_propagates_without_ranking_partial_results(self):
        self.own("A")
        self.own("B")
        first = CardDiscountEstimate(2500, 0, 0, None, None, DiscountReason.APPLIED)
        for failure in (DatabaseError("test failure"), UserCard.DoesNotExist("test failure")):
            with (
                self.subTest(error=type(failure).__name__),
                patch("cards.recommendation_service.estimate_card_discount", side_effect=[first, failure]) as calculate,
                patch("cards.recommendation_service.rank_card_comparisons") as rank,
                self.assertRaises(type(failure)) as error,
            ):
                self.recommend()
            self.assertIs(error.exception, failure)
            self.assertEqual(calculate.call_count, 2)
            rank.assert_not_called()

    def test_reuses_estimator_with_identical_payment_inputs_and_only_reads_data(self):
        a, e = self.own("A"), self.own("E")
        self.pay(e, 50000)
        models = (get_user_model(), Card, UserCard, CardBenefit, Transaction)
        before = [list(model.objects.order_by("pk").values()) for model in models]
        with (
            patch("cards.recommendation_service.estimate_card_discount", wraps=estimate_card_discount) as calculate,
            CaptureQueriesContext(connection) as queries,
        ):
            self.recommend(amount=30000)

        expected = {
            "user_id": self.user.pk, "amount": 30000, "category": Category.FOOD, "as_of": self.as_of,
        }
        for card in (a, e):
            calculate.assert_any_call(**expected, user_card_id=card.pk)
        self.assertEqual(calculate.call_count, 2)
        statements = [query["sql"].split()[0] for query in queries]
        self.assertEqual(statements.count("SELECT"), 8)
        self.assertTrue(set(statements) <= {"SELECT", "SAVEPOINT", "RELEASE"})
        self.assertEqual([list(model.objects.order_by("pk").values()) for model in models], before)
