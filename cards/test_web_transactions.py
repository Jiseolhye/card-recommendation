from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .benefit_calculation import SEOUL
from .models import Card, Category, Transaction, UserCard


AS_OF = datetime(2026, 11, 24, 12, tzinfo=SEOUL)


class TransactionWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.user = users.create_user(username='statement_owner')
        cls.other = users.create_user(username='statement_other')
        cls.empty = users.create_user(username='no_owned_cards')
        cls.cards = {}
        for name in 'ABCDE':
            card = Card.objects.create(name=f'테스트카드 {name}', issuer=f'가상카드사 {name}')
            cls.cards[name] = UserCard.objects.create(user=cls.user, card=card)
        # The other account uses the same catalog product as our account.
        cls.foreign = UserCard.objects.create(user=cls.other, card=cls.cards['A'].card)
        cls.create_payment(cls.foreign, 777_777, AS_OF - timedelta(days=1), '다른 사용자의 거래')
        cls.create_payment(cls.foreign, 1_000, datetime(2026, 9, 12, tzinfo=SEOUL), '타인 전용 월')
        cls.create_payment(cls.cards['C'], 300_000, datetime(2026, 10, 12, tzinfo=SEOUL), '전월 음식점')
        cls.create_payment(cls.cards['A'], 23_000, datetime(2026, 11, 1, 0, 10, tzinfo=SEOUL), '이번 달 첫 거래')
        cls.create_payment(cls.cards['A'], 4_500, datetime(2026, 11, 1, 9, 20, tzinfo=SEOUL), '가상 커피점', Category.CAFE)
        for day in (4, 8, 12):
            cls.create_payment(cls.cards['E'], 50_000, datetime(2026, 11, day, tzinfo=SEOUL), f'가상 음식점 {day}')

    @staticmethod
    def create_payment(card, amount, paid_at, merchant, category=Category.FOOD):
        return Transaction.objects.create(
            user_card=card, amount=amount, transaction_date=paid_at, merchant_name=merchant, category=category,
        )

    def setUp(self):
        self.url = reverse('cards:transaction_list')
        self.client.force_login(self.user)

    def get_history(self, *, as_of=AS_OF, **filters):
        with patch('cards.views.timezone.now', return_value=as_of):
            return self.client.get(self.url, filters)

    def test_login_required_and_get_only(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), f'/login/?next={self.url}', fetch_redirect_response=False)

    def test_default_current_month_uses_session_owner_and_excludes_other_months(self):
        response = self.get_history(user_id=self.other.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'cards/transaction_list.html')
        self.assertEqual(response.context['selected_count'], 5)
        self.assertEqual(response.context['selected_total'], '177,500원')
        self.assertEqual(response.context['current_count'], 5)
        self.assertEqual(response.context['current_total'], '177,500원')
        self.assertEqual(response.context['form']['month'].value(), '2026-11')
        self.assertIsNone(response.context['selected_card'])
        self.assertContains(response, '이번 달 첫 거래')
        self.assertNotContains(response, '다른 사용자의 거래')
        self.assertNotContains(response, '전월 음식점')

    def test_month_dropdown_has_current_and_owned_past_months_and_keeps_filter_values(self):
        response = self.get_history(month='2026-10', card=self.cards['C'].pk)
        form = response.context['form']
        self.assertEqual([value for value, _ in form.fields['month'].widget.choices], ['', '2026-11', '2026-10'])
        self.assertEqual(form['month'].value(), '2026-10')
        self.assertEqual(str(form['card'].value()), str(self.cards['C'].pk))
        self.assertEqual(set(form.fields['card'].queryset.values_list('pk', flat=True)), {owned.pk for owned in self.cards.values()})
        self.assertContains(response, f'<a class="button button-secondary" href="{self.url}">필터 초기화</a>', html=True)

    def test_current_month_card_filters_keep_overall_summary_and_month_comparison(self):
        for card, amount, count in [('A', '27,500원', 2), ('E', '150,000원', 3), ('B', '0원', 0)]:
            with self.subTest(card=card):
                response = self.get_history(month='2026-11', card=self.cards[card].pk)
                self.assertEqual(response.context['selected_total'], amount)
                self.assertEqual(response.context['selected_count'], count)
                self.assertEqual(response.context['current_total'], '177,500원')
                self.assertEqual(response.context['current_count'], 5)
                self.assertEqual(len(response.context['card_totals']), 5)
        self.assertContains(response, '조회된 이용내역이 없어요.')
        self.assertContains(response, '다른 카드 또는 조회 월을 선택해 보세요.')

    def test_past_month_totals_for_all_cards_and_individual_cards(self):
        for card, amount, count in [('', '300,000원', 1), (self.cards['C'].pk, '300,000원', 1), (self.cards['E'].pk, '0원', 0)]:
            with self.subTest(card=card):
                response = self.get_history(month='2026-10', card=card)
                self.assertEqual(response.context['selected_total'], amount)
                self.assertEqual(response.context['selected_count'], count)
                self.assertEqual(response.context['current_total'], '177,500원')
                self.assertEqual(response.context['month_total'], 300_000)
                self.assertNotContains(response, '이번 달 첫 거래')

    def test_card_comparison_uses_month_total_and_sorts_zero_ties_by_pk(self):
        response = self.get_history(card=self.cards['E'].pk)
        rows = response.context['card_totals']
        self.assertEqual([row['id'] for row in rows], [self.cards[name].pk for name in 'EABCD'])
        self.assertEqual([row['formatted_amount'] for row in rows], ['150,000원', '27,500원', '0원', '0원', '0원'])
        self.assertEqual([row['share'] for row in rows], ['84.5', '15.5', '0', '0', '0'])
        self.assertContains(response, '<meter min="0" max="177500" value="150000"')

    def test_valid_unlisted_past_month_has_zero_meters_and_visible_empty_state(self):
        response = self.get_history(month='2026-08')
        self.assertContains(response, '조회된 이용내역이 없어요.')
        self.assertContains(response, '<meter min="0" max="1" value="0"', count=5)
        self.assertEqual(response.context['selected_total'], '0원')
        self.assertEqual(response.context['selected_count'], 0)
        self.assertIn(('2026-08', '2026년 8월'), response.context['form'].fields['month'].widget.choices)

    def test_no_cards_and_no_saved_transactions_have_distinct_empty_states(self):
        self.client.force_login(self.empty)
        response = self.get_history()
        self.assertContains(response, '아직 등록된 카드가 없어요.')
        self.assertContains(response, f'{reverse("cards:card_list")}#add-card')
        UserCard.objects.create(user=self.empty, card=self.cards['D'].card)
        response = self.get_history()
        self.assertContains(response, '아직 저장된 이용내역이 없어요.')
        self.assertEqual(response.context['current_total'], '0원')
        self.assertEqual(response.context['current_count'], 0)

    def test_invalid_month_including_future_shows_field_error_without_defaulting(self):
        for month in ('', '2026-1', '2026-00', '2026-13', '0000-12', '0001-01', '2026-12', '2026-11' * 10):
            with self.subTest(month=month):
                response = self.get_history(month=month)
                self.assertEqual(response.status_code, 400)
                self.assertIn('month', response.context['form'].errors)
                self.assertNotContains(response, '이번 달 첫 거래', status_code=400)
                self.assertNotIn('transaction_groups', response.context)
        self.assertContains(self.get_history(month='2026-12'), '미래의 이용내역은 조회할 수 없어요.', status_code=400)

    def test_invalid_card_ids_reject_tampering_and_do_not_show_owned_or_foreign_transactions(self):
        for card in (self.foreign.pk, 999999, 'not-an-integer'):
            with self.subTest(card=card):
                response = self.get_history(card=card)
                self.assertContains(response, '보유한 카드 중에서 선택해 주세요.', status_code=400)
                self.assertNotContains(response, '이번 달 첫 거래', status_code=400)
                self.assertNotContains(response, '다른 사용자의 거래', status_code=400)
                self.assertNotIn('transaction_groups', response.context)

    def test_seoul_month_start_inclusive_and_next_month_and_as_of_exclusive(self):
        start = datetime(2026, 10, 31, 15, tzinfo=UTC)
        self.create_payment(self.cards['A'], 10_000, start - timedelta(microseconds=1), '10월 마지막 순간')
        self.create_payment(self.cards['A'], 20_000, start, '11월 시작 정각')
        self.create_payment(self.cards['A'], 30_000, AS_OF, '기준 시각 정각')
        self.create_payment(self.cards['A'], 40_000, AS_OF + timedelta(seconds=1), '기준 시각 이후')
        november = self.get_history()
        october = self.get_history(month='2026-10')
        self.assertEqual(november.context['selected_total'], '197,500원')
        self.assertEqual(october.context['selected_total'], '310,000원')
        self.assertContains(november, '11월 시작 정각')
        self.assertNotContains(october, '11월 시작 정각')
        self.assertNotContains(november, '기준 시각 정각')
        self.assertNotContains(november, '기준 시각 이후')

    def test_year_boundary_from_december_to_january_in_seoul(self):
        start = datetime(2026, 12, 31, 15, tzinfo=UTC)
        self.create_payment(self.cards['B'], 12_000, start - timedelta(seconds=1), '12월 결제')
        self.create_payment(self.cards['B'], 34_000, start, '1월 결제')
        as_of = start + timedelta(days=3)
        with patch('cards.views.timezone.now', return_value=as_of):
            self.client.force_login(self.user)
        january = self.get_history(as_of=as_of)
        december = self.get_history(as_of=as_of, month='2026-12')
        self.assertEqual(january.context['form']['month'].value(), '2027-01')
        self.assertEqual(january.context['selected_total'], '34,000원')
        self.assertEqual(december.context['selected_total'], '12,000원')
        self.assertEqual(december.context['current_total'], '34,000원')

    def test_latest_first_then_descending_pk_with_seoul_display_and_safe_markup(self):
        paid_at = datetime(2026, 11, 20, 18, 15, tzinfo=UTC)
        older_id = self.create_payment(self.cards['A'], 10_000, paid_at, '먼저 저장된 결제')
        newer_id = self.create_payment(self.cards['A'], 15_000, paid_at, '<script>가맹점</script>')
        response = self.get_history()
        first_day = response.context['transaction_groups'][0]
        self.assertEqual(first_day['date'].isoformat(), '2026-11-21')
        self.assertEqual([entry['payment'].pk for entry in first_day['entries']], [newer_id.pk, older_id.pk])
        self.assertContains(response, '2026-11-21T03:15:00+09:00')
        self.assertContains(response, '>03:15</time>')
        self.assertContains(response, '카페')
        self.assertContains(response, '&lt;script&gt;가맹점&lt;/script&gt;')
        self.assertNotContains(response, '<script>가맹점</script>')

    def test_home_summary_links_and_detail_prefilter_match_history(self):
        with patch('cards.views.timezone.now', return_value=AS_OF):
            home = self.client.get(reverse('cards:home'))
            detail = self.client.get(reverse('cards:card_detail', args=[self.cards['E'].pk]))
        history = self.get_history(card=self.cards['E'].pk)
        self.assertEqual(home.context['monthly_spending'], history.context['current_total'])
        self.assertContains(home, '이용내역 보기')
        self.assertContains(detail, f'href="{self.url}?card={self.cards["E"].pk}"')
        self.assertContains(history, f'href="{self.url}" aria-current="page"')
        self.assertEqual(history.context['selected_card'].pk, self.cards['E'].pk)
        self.assertEqual(history.context['form']['month'].value(), '2026-11')

    def test_valid_and_invalid_requests_are_read_only(self):
        before_transactions = list(Transaction.objects.order_by('pk').values())
        before_cards = list(UserCard.objects.order_by('pk').values())
        self.get_history(month='2026-10')
        self.get_history(card=self.foreign.pk)
        self.get_history(month='2026-99')
        self.assertEqual(list(Transaction.objects.order_by('pk').values()), before_transactions)
        self.assertEqual(list(UserCard.objects.order_by('pk').values()), before_cards)

    def test_query_count_does_not_grow_with_more_transaction_rows(self):
        with CaptureQueriesContext(connection) as few:
            self.assertEqual(self.get_history().status_code, 200)
        for _ in range(8):
            self.create_payment(self.cards['A'], 1, AS_OF - timedelta(hours=1), '가상 가맹점')
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(self.get_history().status_code, 200)
        self.assertEqual(len(few), len(many))

    def test_database_failure_returns_generic_retry_message(self):
        with patch('cards.views.current_user_spending', side_effect=DatabaseError('private query information')):
            response = self.get_history()
        self.assertContains(response, '이용내역을 불러오지 못했어요.', status_code=503)
        self.assertNotContains(response, 'private query information', status_code=503)
