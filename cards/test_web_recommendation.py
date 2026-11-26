from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse

from .benefit_calculation import SEOUL, month_bounds
from .models import Card, CardBenefit, Category, Transaction, UserCard
from .recommendation_service import recommend_best_card


AS_OF = datetime(2026, 11, 24, 12, tzinfo=SEOUL)


class RecommendationWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_mvp_cards', verbosity=0, stdout=StringIO())
        users = get_user_model().objects
        cls.user = users.create_user(username='recommendation_owner')
        cls.other = users.create_user(username='no_cards')
        cls.cards = {
            card.name[-1]: UserCard.objects.create(user=cls.user, card=card)
            for card in Card.objects.order_by('name')
        }
        cls.add_payment(cls.cards['C'], 300_000, datetime(2026, 10, 12, tzinfo=SEOUL))
        cls.add_payment(cls.cards['A'], 23_000, datetime(2026, 11, 1, 0, 10, tzinfo=SEOUL))
        cls.add_payment(cls.cards['A'], 4_500, datetime(2026, 11, 1, 9, 20, tzinfo=SEOUL), Category.CAFE)
        for day in (4, 8, 12):
            cls.add_payment(cls.cards['E'], 50_000, datetime(2026, 11, day, tzinfo=SEOUL))

    @staticmethod
    def add_payment(card, amount, date, category=Category.FOOD):
        return Transaction.objects.create(
            user_card=card, amount=amount, transaction_date=date, category=category, merchant_name='가상 가맹점',
        )

    def setUp(self):
        self.url = reverse('cards:home')
        self.client.force_login(self.user)

    def recommend(self, amount='50000', category=Category.FOOD, **data):
        with patch('cards.views.timezone.now', return_value=AS_OF):
            return self.client.post(self.url, {'amount': amount, 'category': category, **data})

    def test_anonymous_home_get_and_post_redirect_to_login(self):
        self.client.logout()
        for response in (self.client.get(self.url), self.client.post(self.url)):
            self.assertRedirects(response, '/login/?next=/')

    def test_dashboard_applies_seoul_month_boundary_owner_and_exclusive_cutoff(self):
        start = month_bounds(AS_OF).current_start
        foreign = UserCard.objects.create(user=self.other, card=self.cards['A'].card)
        self.add_payment(foreign, 999_999, start)
        self.add_payment(self.cards['A'], 10_000, start - timedelta(microseconds=1))
        self.add_payment(self.cards['A'], 2_000, start)
        self.add_payment(self.cards['A'], 900_000, AS_OF)
        self.add_payment(self.cards['A'], 800_000, AS_OF + timedelta(seconds=1))
        with patch('cards.views.timezone.now', return_value=AS_OF):
            response = self.client.get(self.url)
        self.assertContains(response, '5장')
        self.assertContains(response, '179,500원')
        self.assertContains(response, '결제 업종과 금액을 입력하면 보유 카드의 예상 혜택을 비교해 드려요.')
        self.assertEqual(dict(response.context['form'].fields['category'].choices), {'': '업종 선택', **dict(Category.choices)})

    def test_recommendation_uses_session_user_validated_inputs_and_same_timestamp_for_spending(self):
        with patch('cards.views.recommend_best_card', wraps=recommend_best_card) as recommend, \
             patch('cards.views.current_user_spending', return_value=177_500) as spending:
            response = self.recommend(user_id=self.other.pk)
        recommend.assert_called_once_with(user_id=self.user.pk, amount=50_000, category=Category.FOOD, as_of=AS_OF)
        spending.assert_called_once_with(user_id=self.user.pk, as_of=AS_OF)
        self.assertContains(response, '6,000원')

    def test_five_card_result_keeps_server_order_and_does_not_write(self):
        before_transactions = list(Transaction.objects.order_by('pk').values())
        before_cards = list(UserCard.objects.order_by('pk').values())
        response = self.recommend()
        recommendation = response.context['recommendation']
        self.assertEqual(recommendation.recommended_card.user_card_id, self.cards['D'].pk)
        result_rows = [row['card'] for row in response.context['comparisons']]
        self.assertEqual(result_rows, list(recommendation.comparisons))
        self.assertEqual([(row.card_name[-1], row.estimate.estimated_discount) for row in result_rows],
                         [('D', 6_000), ('C', 5_000), ('B', 4_000), ('A', 2_500), ('E', 1_000)])
        self.assertContains(response, '44,000원')
        self.assertContains(response, '차순위 카드 대비 추가 절약 예상액')
        self.assertEqual(response.context['additional_savings'], '1,000원')
        self.assertContains(response, '<meter min="0" max="6000" value="6000"', count=1)
        self.assertEqual(list(Transaction.objects.order_by('pk').values()), before_transactions)
        self.assertEqual(list(UserCard.objects.order_by('pk').values()), before_cards)

    def test_zero_invalid_and_oversized_amounts_and_invalid_categories_skip_recommendation(self):
        samples = [(amount, Category.FOOD, 'amount') for amount in ('', '0', '-1', '1.0', '1e4', 'abc', '9' * 24, ' 10 ')]
        samples.extend([('50000', '', 'category'), ('50000', 'WRONG', 'category')])
        with patch('cards.views.recommend_best_card') as recommend:
            for amount, category, field in samples:
                with self.subTest(amount=amount, category=category):
                    response = self.recommend(amount=amount, category=category)
                    self.assertIn(field, response.context['form'].errors)
        recommend.assert_not_called()
        self.assertContains(response, 'value="50000"')

    def test_largest_form_input_keeps_whole_won_calculation_exact(self):
        CardBenefit.objects.filter(card=self.cards['D'].card, category=Category.FOOD).update(
            benefit_rate=Decimal('100.00'), minimum_amount=0,
        )
        for amount in ('1', '99999999', '100000000'):
            with self.subTest(amount=amount):
                response = self.recommend(amount=amount)
                self.assertEqual(response.context['recommendation'].estimated_discount, int(amount))
                self.assertEqual(response.context['estimated_payment'], '0원')

    def test_payment_above_web_cap_displays_field_error_without_recommending(self):
        with patch('cards.views.recommend_best_card') as recommend:
            for amount in ('100000001', '9' * 23):
                with self.subTest(amount=amount):
                    response = self.recommend(amount=amount)
                    self.assertTrue(response.context['form'].has_error('amount', 'max_value'))
                    self.assertContains(response, '결제 예정 금액은 100,000,000원까지 입력할 수 있어요.')
                    self.assertContains(response, '1원부터 1억원까지 입력할 수 있어요.')
                    self.assertContains(response, f'value="{amount}"')
        recommend.assert_not_called()

    def test_no_cards_shows_zero_summary_and_first_registration_link(self):
        self.client.force_login(self.other)
        response = self.recommend()
        self.assertContains(response, '0장')
        self.assertContains(response, '0원')
        self.assertContains(response, '첫 카드 등록')
        self.assertEqual(response.context['recommendation'].reason, 'no_cards')
        self.assertNotContains(response, '<meter')

    def test_all_zero_keeps_every_card_and_explains_no_matching_category(self):
        response = self.recommend(category=Category.CAFE)
        self.assertContains(response, '이번 결제에 예상 할인이 적용되는 보유 카드가 없습니다.')
        self.assertContains(response, '선택한 업종의 할인 혜택이 없어요.', count=5)
        self.assertContains(response, '<meter min="0" max="1" value="0"', count=5)
        self.assertEqual(len(response.context['comparisons']), 5)

    def test_all_highest_positive_ties_are_joint_first_with_zero_difference(self):
        UserCard.objects.create(user=self.other, card=self.cards['A'].card)
        capped = UserCard.objects.create(user=self.other, card=self.cards['E'].card)
        for day in (4, 8, 12):
            self.add_payment(capped, 50_000, datetime(2026, 11, day, tzinfo=SEOUL))
        self.client.force_login(self.other)
        response = self.recommend(amount='20000')
        self.assertContains(response, '공동 1위', count=2)
        self.assertContains(response, '같은 예상 할인 혜택을 받을 수 있는 카드가 여러 장 있어요.')
        self.assertContains(response, '차순위 카드 대비 차액')
        self.assertNotContains(response, '차순위 카드 대비 추가 절약 예상액')
        self.assertEqual(response.context['additional_savings'], '0원')

    def test_single_owned_card_has_no_runner_up_comparison(self):
        UserCard.objects.create(user=self.other, card=self.cards['B'].card)
        self.client.force_login(self.other)
        response = self.recommend()
        self.assertContains(response, '4,000원')
        self.assertNotContains(response, '차순위 카드 대비')
        self.assertIsNone(response.context['recommendation'].additional_savings)

    def test_condition_labels_are_evaluated_independently_of_first_reason(self):
        CardBenefit.objects.filter(card=self.cards['D'].card).update(required_spending=300_000)
        response = self.recommend(amount='10000')
        row = next(row for row in response.context['comparisons'] if row['card'].user_card_id == self.cards['D'].pk)
        self.assertEqual(row['reason'], '지난달 이용금액이 혜택 기준에 미치지 않아요.')
        self.assertEqual(row['spending_status'], '미충족')
        self.assertEqual(row['minimum_status'], '미충족')

    def test_recoverable_error_shows_generic_retry_and_preserves_input(self):
        with patch('cards.views.recommend_best_card', side_effect=DatabaseError('internal detail')):
            response = self.recommend()
        self.assertContains(response, '추천 정보를 불러오지 못했어요.', status_code=503)
        self.assertContains(response, 'value="50000"', status_code=503)
        self.assertNotContains(response, 'internal detail', status_code=503)

    def test_unexpected_failure_returns_generic_500_and_logs_only_the_error_type(self):
        with patch('cards.views.recommend_best_card', side_effect=RuntimeError('private detail')):
            with self.assertLogs('cards.views', level='ERROR') as logs:
                response = self.recommend()
        self.assertContains(response, '추천 정보를 불러오지 못했어요.', status_code=500)
        self.assertNotContains(response, 'private detail', status_code=500)
        self.assertNotContains(response, '예상 할인금액', status_code=500)
        self.assertIn('RuntimeError', logs.output[0])
        self.assertNotIn('private detail', logs.output[0])
