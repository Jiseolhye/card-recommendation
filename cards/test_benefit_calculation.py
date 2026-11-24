from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .benefit_calculation import (
    Discount,
    DiscountReason,
    DiscountTerms,
    HistoricalPayment,
    MonthBounds,
    calculate_discount,
    estimate_monthly_discount_used,
    month_bounds,
)
from .benefit_service import CardDiscountEstimate, estimate_card_discount
from .models import Card, CardBenefit, Category, Transaction, UserCard


class DiscountCalculationTests(SimpleTestCase):
    def setUp(self):
        self.terms = DiscountTerms(Category.FOOD, Decimal("5.00"), 0, 0, None, None)

    def discount(self, *, amount=50000, terms=None, previous=0, used=0):
        return calculate_discount(
            amount=amount,
            category=Category.FOOD,
            terms=self.terms if terms is None else terms,
            previous_month_spending=previous,
            estimated_monthly_used=used,
        )

    def test_seoul_month_bounds_include_year_rollover(self):
        seoul = ZoneInfo("Asia/Seoul")
        self.assertEqual(
            month_bounds(datetime(2027, 1, 15, tzinfo=seoul)),
            MonthBounds(
                previous_start=datetime(2026, 11, 30, 15, tzinfo=UTC),
                current_start=datetime(2026, 12, 31, 15, tzinfo=UTC),
                next_start=datetime(2027, 1, 31, 15, tzinfo=UTC),
            ),
        )
        self.assertEqual(
            month_bounds(datetime(2026, 11, 30, 14, 59, 59, tzinfo=UTC)).current_start,
            datetime(2026, 10, 31, 15, tzinfo=UTC),
        )

    def test_rounds_down_in_whole_won_with_unlimited_caps(self):
        self.assertEqual(self.discount(amount=19999), Discount(999, DiscountReason.APPLIED))
        self.assertEqual(self.discount(amount=1), Discount(0, DiscountReason.ROUNDED_TO_ZERO))

    def test_zero_caps_are_distinct_from_unlimited_caps(self):
        for cap, reason in (
            ("monthly_limit", DiscountReason.MONTHLY_LIMIT_EXHAUSTED),
            ("per_transaction_limit", DiscountReason.PER_TRANSACTION_LIMIT_ZERO),
        ):
            with self.subTest(cap=cap):
                self.assertEqual(
                    self.discount(terms=replace(self.terms, **{cap: 0})),
                    Discount(0, reason),
                )

    def test_reason_priority_for_multiple_failed_conditions(self):
        terms = replace(self.terms, minimum_amount=20000, required_spending=300000, monthly_limit=0)
        self.assertEqual(
            calculate_discount(
                amount=10000, category=Category.CAFE, terms=terms, previous_month_spending=0,
            ),
            Discount(0, DiscountReason.NO_MATCHING_BENEFIT),
        )
        self.assertEqual(
            self.discount(amount=10000, terms=terms),
            Discount(0, DiscountReason.PREVIOUS_SPENDING_NOT_MET),
        )
        self.assertEqual(
            self.discount(amount=10000, terms=terms, previous=300000),
            Discount(0, DiscountReason.MINIMUM_AMOUNT_NOT_MET),
        )
        self.assertEqual(
            self.discount(terms=replace(terms, per_transaction_limit=0), previous=300000),
            Discount(0, DiscountReason.MONTHLY_LIMIT_EXHAUSTED),
        )

    def test_unlimited_month_does_not_evaluate_history(self):
        def payments():
            self.fail("Unlimited benefits do not need transaction history.")
            yield

        self.assertIsNone(estimate_monthly_discount_used(
            terms=self.terms, previous_month_spending=0, payments=payments(),
        ))

    def test_replay_orders_by_time_then_id_and_caps_the_last_discount(self):
        terms = replace(self.terms, monthly_limit=2000)
        same_time = datetime(2026, 11, 10, 1, tzinfo=UTC)
        rows = [
            HistoricalPayment(same_time, 3, 16000, Category.FOOD),
            HistoricalPayment(same_time, 2, 15000, Category.FOOD),
            HistoricalPayment(same_time - timedelta(seconds=1), 4, 20000, Category.FOOD),
            HistoricalPayment(same_time + timedelta(seconds=1), 1, 50000, Category.FOOD),
        ]
        allocations = []

        def record_discount(**inputs):
            discount = calculate_discount(**inputs)
            allocations.append((inputs["amount"], discount.amount))
            return discount

        with patch("cards.benefit_calculation.calculate_discount", side_effect=record_discount):
            self.assertEqual(
                estimate_monthly_discount_used(terms=terms, previous_month_spending=0, payments=rows),
                2000,
            )
        self.assertEqual(allocations, [(20000, 1000), (15000, 750), (16000, 250)])

    def test_replay_rounds_each_transaction_before_accumulating(self):
        terms = replace(self.terms, monthly_limit=100)
        when = datetime(2026, 11, 10, tzinfo=UTC)
        rows = [HistoricalPayment(when, pk, 199, Category.FOOD) for pk in (1, 2)]
        self.assertEqual(
            estimate_monthly_discount_used(terms=terms, previous_month_spending=0, payments=rows),
            18,
        )


class CardDiscountServiceTests(TestCase):
    as_of = datetime(2026, 11, 15, 3, tzinfo=UTC)
    earlier_this_month = datetime(2026, 11, 10, 3, tzinfo=UTC)
    previous_month = datetime(2026, 10, 15, 3, tzinfo=UTC)

    @classmethod
    def setUpTestData(cls):
        call_command("seed_mvp_cards", stdout=StringIO())
        cls.user = get_user_model().objects.create_user(username="benefit-learner")
        cls.other_user = get_user_model().objects.create_user(username="other-learner")
        cls.user_cards = {
            suffix: UserCard.objects.create(
                user=cls.user, card=Card.objects.get(name=f"테스트카드 {suffix}"),
            )
            for suffix in "ABCDE"
        }

    def pay(self, suffix, amount, *, when=None, category=Category.FOOD, user_card=None):
        return Transaction.objects.create(
            user_card=self.user_cards[suffix] if user_card is None else user_card,
            amount=amount,
            category=category,
            transaction_date=self.earlier_this_month if when is None else when,
            merchant_name="가상 가맹점",
        )

    def estimate(self, suffix, **overrides):
        inputs = {
            "user_id": self.user.pk,
            "user_card_id": self.user_cards[suffix].pk,
            "amount": 50000,
            "category": Category.FOOD,
            "as_of": self.as_of,
        }
        inputs.update(overrides)
        return estimate_card_discount(**inputs)

    def test_card_a_applies_five_percent_with_no_transactions(self):
        self.assertEqual(
            self.estimate("A"), CardDiscountEstimate(2500, 0, 0, None, None, DiscountReason.APPLIED),
        )

    def test_card_b_applies_eight_percent(self):
        self.assertEqual(self.estimate("B").estimated_discount, 4000)

    def test_card_c_without_transaction_history_has_zero_spending_and_no_discount(self):
        self.assertEqual(
            self.estimate("C"),
            CardDiscountEstimate(0, 0, 0, None, None, DiscountReason.PREVIOUS_SPENDING_NOT_MET),
        )

    def test_card_c_previous_month_threshold_counts_all_categories(self):
        self.pay("C", 299999, when=self.previous_month, category=Category.CAFE)
        self.pay("C", 100000, category=Category.OTHER)
        self.assertEqual(
            self.estimate("C"),
            CardDiscountEstimate(0, 100000, 299999, None, None, DiscountReason.PREVIOUS_SPENDING_NOT_MET),
        )
        self.pay("C", 1, when=self.previous_month)
        self.assertEqual(
            self.estimate("C"),
            CardDiscountEstimate(5000, 100000, 300000, None, None, DiscountReason.APPLIED),
        )

    def test_card_d_minimum_amount_boundary(self):
        below = self.estimate("D", amount=19999)
        self.assertEqual((below.estimated_discount, below.reason), (0, DiscountReason.MINIMUM_AMOUNT_NOT_MET))
        self.assertEqual(self.estimate("D", amount=20000).estimated_discount, 2400)

    def test_card_e_per_transaction_limit_with_no_history(self):
        self.assertEqual(
            self.estimate("E"), CardDiscountEstimate(3000, 0, 0, 0, 10000, DiscountReason.APPLIED),
        )

    def test_card_e_three_prior_payments_leave_one_thousand_won(self):
        for _ in range(3):
            self.pay("E", 50000)
        self.assertEqual(
            self.estimate("E", amount=30000),
            CardDiscountEstimate(1000, 150000, 0, 9000, 1000, DiscountReason.APPLIED),
        )

    def test_card_e_monthly_limit_exhausted(self):
        for _ in range(4):
            self.pay("E", 50000)
        self.assertEqual(
            self.estimate("E", amount=30000),
            CardDiscountEstimate(0, 200000, 0, 10000, 0, DiscountReason.MONTHLY_LIMIT_EXHAUSTED),
        )

    def test_cafe_has_no_benefit_but_spending_is_returned_for_all_mvp_cards(self):
        for suffix in "ABCDE":
            with self.subTest(card=suffix):
                self.pay(suffix, 300000, when=self.previous_month)
                self.pay(suffix, 50000, category=Category.CAFE)
                self.assertEqual(
                    self.estimate(suffix, category=Category.CAFE),
                    CardDiscountEstimate(0, 50000, 300000, None, None, DiscountReason.NO_MATCHING_BENEFIT),
                )

    def test_korean_midnight_and_december_to_january_partition_transactions(self):
        self.pay("A", 999, when=datetime(2026, 11, 30, 14, 59, 59, tzinfo=UTC))
        self.pay("A", 100, when=datetime(2026, 11, 30, 15, tzinfo=UTC))
        self.pay("A", 200, when=datetime(2026, 12, 31, 14, 59, 59, tzinfo=UTC))
        self.pay("A", 300, when=datetime(2026, 12, 31, 15, tzinfo=UTC))
        result = self.estimate("A", as_of=datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("Asia/Seoul")))
        self.assertEqual((result.previous_month_spending, result.current_month_spending), (300, 300))

    def test_only_the_selected_owned_card_contributes_to_spending_and_usage(self):
        other_card = UserCard.objects.create(user=self.other_user, card=self.user_cards["E"].card)
        self.pay("E", 50000, user_card=other_card)
        self.pay("A", 70000)
        self.pay("E", 10000)
        self.pay("E", 20000, when=self.previous_month)
        self.pay("E", 90000, when=self.previous_month, user_card=other_card)
        result = self.estimate("E")
        self.assertEqual(
            (result.current_month_spending, result.previous_month_spending, result.estimated_monthly_discount_used),
            (10000, 20000, 1500),
        )

    def test_cutoff_excludes_transactions_at_and_after_the_proposed_payment(self):
        for seconds in (-1, 0, 1):
            self.pay("E", 50000, when=self.as_of + timedelta(seconds=seconds))
        result = self.estimate("E")
        self.assertEqual((result.current_month_spending, result.estimated_monthly_discount_used), (50000, 3000))

    def test_month_limit_resets_at_korean_midnight(self):
        boundary = datetime(2026, 11, 30, 15, tzinfo=UTC)
        for _ in range(3):
            self.pay("E", 50000, when=boundary - timedelta(seconds=1))
        before = self.estimate("E", as_of=boundary - timedelta(microseconds=1))
        after = self.estimate("E", as_of=boundary)
        self.assertEqual((before.estimated_monthly_discount_used, before.remaining_monthly_limit), (9000, 1000))
        self.assertEqual(after, CardDiscountEstimate(3000, 0, 150000, 0, 10000, DiscountReason.APPLIED))

    def test_benefits_for_different_categories_use_independent_monthly_limits(self):
        CardBenefit.objects.create(
            card=self.user_cards["E"].card,
            category=Category.CAFE,
            benefit_rate=Decimal("10.00"),
            monthly_limit=5000,
        )
        self.pay("E", 50000)
        self.pay("E", 40000, category=Category.CAFE)
        food = self.estimate("E")
        cafe = self.estimate("E", category=Category.CAFE)
        self.assertEqual(food, CardDiscountEstimate(3000, 90000, 0, 3000, 7000, DiscountReason.APPLIED))
        self.assertEqual(cafe, CardDiscountEstimate(1000, 90000, 0, 4000, 1000, DiscountReason.APPLIED))

    def test_replay_uses_previous_month_spending_and_minimum_for_each_payment(self):
        CardBenefit.objects.create(
            card=self.user_cards["E"].card,
            category=Category.MART,
            benefit_rate=Decimal("10.00"),
            minimum_amount=20000,
            required_spending=300000,
            monthly_limit=10000,
            per_transaction_limit=3000,
        )
        self.pay("E", 299999, when=self.previous_month, category=Category.CAFE)
        self.pay("E", 19999, category=Category.MART)
        self.pay("E", 20000, category=Category.MART)
        self.pay("E", 50000, category=Category.MART)
        self.pay("E", 60000)
        self.assertEqual(
            self.estimate("E", category=Category.MART),
            CardDiscountEstimate(0, 149999, 299999, 0, 10000, DiscountReason.PREVIOUS_SPENDING_NOT_MET),
        )
        self.pay("E", 1, when=self.previous_month)
        self.assertEqual(
            self.estimate("E", category=Category.MART),
            CardDiscountEstimate(3000, 149999, 300000, 5000, 5000, DiscountReason.APPLIED),
        )

    def test_non_positive_and_non_integer_amounts_are_rejected(self):
        for amount in (0, -1, True, False, 50000.0, "50000", Decimal("50000"), None):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.estimate("A", amount=amount)

    def test_invalid_category_is_rejected(self):
        for category in ("SHOPPING", "food", "", None):
            with self.subTest(category=category), self.assertRaises(ValueError):
                self.estimate("A", category=category)

    def test_naive_and_non_datetime_as_of_are_rejected(self):
        for as_of in (self.as_of.replace(tzinfo=None), self.as_of.date(), self.as_of.isoformat(), None):
            with self.subTest(as_of=as_of), self.assertRaises(ValueError):
                self.estimate("A", as_of=as_of)

    def test_missing_user_card_raises_does_not_exist(self):
        with self.assertRaises(UserCard.DoesNotExist):
            self.estimate("A", user_card_id=99999)

    def test_foreign_user_card_raises_does_not_exist(self):
        with self.assertRaises(UserCard.DoesNotExist):
            self.estimate("A", user_id=self.other_user.pk)

    def test_service_is_read_only_and_query_count_does_not_scale_with_history(self):
        for _ in range(10):
            self.pay("E", 1000)
        models = (get_user_model(), Card, UserCard, CardBenefit, Transaction)
        before = [list(model.objects.order_by("pk").values()) for model in models]
        with CaptureQueriesContext(connection) as queries:
            result = self.estimate("E")
        self.assertEqual(result.estimated_monthly_discount_used, 1500)
        statements = [query["sql"].split()[0] for query in queries]
        self.assertEqual(statements.count("SELECT"), 4)
        self.assertTrue(set(statements) <= {"SELECT", "SAVEPOINT", "RELEASE"})
        self.assertEqual([list(model.objects.order_by("pk").values()) for model in models], before)
