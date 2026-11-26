from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .benefit_calculation import SEOUL
from .benefit_service import estimate_card_discount
from .models import Card, CardBenefit, Category, Transaction, UserCard


AS_OF = datetime(2026, 11, 24, 12, tzinfo=SEOUL)


class CardWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.user = users.create_user(username='card_owner')
        cls.other = users.create_user(username='other_owner')
        cls.product = Card.objects.create(name='내 테스트카드', issuer='가상카드사')
        cls.available = Card.objects.create(name='등록 가능한 카드', issuer='가상카드사')
        cls.owned = UserCard.objects.create(user=cls.user, card=cls.product)
        cls.foreign = UserCard.objects.create(user=cls.other, card=cls.available)
        CardBenefit.objects.create(
            card=cls.product, category=Category.FOOD, benefit_rate=Decimal('15.00'),
            required_spending=300_000, minimum_amount=20_000,
            per_transaction_limit=3_000, monthly_limit=10_000,
        )
        CardBenefit.objects.create(card=cls.product, category=Category.CAFE, benefit_rate=Decimal('5.00'))

    def setUp(self):
        self.client.force_login(self.user)
        self.list_url = reverse('cards:card_list')
        self.add_url = reverse('cards:card_add')
        self.detail_url = reverse('cards:card_detail', args=[self.owned.pk])
        self.delete_url = reverse('cards:card_delete', args=[self.owned.pk])

    def payment(self, card, amount, date):
        return Transaction.objects.create(
            user_card=card, amount=amount, transaction_date=date,
            category=Category.FOOD, merchant_name='가상 음식점',
        )

    def test_card_pages_and_actions_require_login(self):
        client = Client()
        for method, url in [('get', self.list_url), ('get', self.detail_url),
                            ('post', self.add_url), ('post', self.delete_url)]:
            with self.subTest(url=url):
                self.assertRedirects(getattr(client, method)(url), f'/login/?next={url}')

    def test_list_contains_only_owned_links_and_excludes_owned_products_from_choices(self):
        response = self.client.get(self.list_url)
        self.assertContains(response, '등록된 보유 카드 1장')
        self.assertContains(response, '음식점 15% 할인')
        self.assertContains(response, '카페 5% 할인')
        self.assertContains(response, self.detail_url)
        self.assertNotContains(response, reverse('cards:card_detail', args=[self.foreign.pk]))
        self.assertEqual(list(response.context['form'].fields['card'].queryset), [self.available])

    def test_registers_only_an_existing_product_for_the_session_user(self):
        response = self.client.post(self.add_url, {'card': self.available.pk, 'user_id': self.other.pk}, follow=True)
        self.assertRedirects(response, self.list_url)
        self.assertContains(response, '내 카드에 등록했어요.')
        self.assertTrue(UserCard.objects.filter(user=self.user, card=self.available).exists())
        self.assertEqual(Card.objects.count(), 2)

    def test_duplicate_and_nonexistent_products_do_not_create_owned_cards(self):
        for product_id in (self.product.pk, 999999):
            with self.subTest(product_id=product_id):
                response = self.client.post(self.add_url, {'card': product_id})
                self.assertEqual(response.context['form'].errors.as_data()['card'][0].code, 'invalid_choice')
        self.assertEqual(self.user.user_cards.count(), 1)

    def test_reports_duplicate_if_registered_after_form_validation(self):
        with patch('cards.views.AddCardForm.is_valid', autospec=True) as is_valid:
            def register_during_validation(form):
                form.full_clean()
                UserCard.objects.create(user=self.user, card=self.available)
                return True

            is_valid.side_effect = register_during_validation
            response = self.client.post(self.add_url, {'card': self.available.pk})
        self.assertContains(response, '이미 등록한 카드예요.')
        self.assertEqual(self.user.user_cards.filter(card=self.available).count(), 1)

    def test_shows_card_name_in_delete_confirmation_and_deletes_only_the_owned_link(self):
        self.assertContains(self.client.get(self.list_url), '카드를 내 카드에서 삭제할까요?')
        response = self.client.post(self.delete_url, follow=True)
        self.assertRedirects(response, self.list_url)
        self.assertContains(response, '내 카드에서 삭제했어요.')
        self.assertFalse(UserCard.objects.filter(pk=self.owned.pk).exists())
        self.assertTrue(Card.objects.filter(pk=self.product.pk).exists())

    def test_foreign_and_missing_card_details_and_deletions_return_404(self):
        for pk in (self.foreign.pk, 999999):
            with self.subTest(pk=pk):
                self.assertEqual(self.client.get(reverse('cards:card_detail', args=[pk])).status_code, 404)
                self.assertEqual(self.client.post(reverse('cards:card_delete', args=[pk])).status_code, 404)
        self.assertTrue(UserCard.objects.filter(pk=self.foreign.pk).exists())

    def test_transaction_protection_preserves_card_and_transaction(self):
        payment = self.payment(self.owned, 30_000, AS_OF - timedelta(days=1))
        self.assertContains(self.client.get(self.list_url), '이용내역이 연결되어 삭제할 수 없어요.')
        response = self.client.post(self.delete_url, follow=True)
        self.assertContains(response, '이용내역이 연결된 카드는 삭제할 수 없어요.')
        self.assertTrue(UserCard.objects.filter(pk=self.owned.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=payment.pk).exists())

    def test_registration_and_deletion_are_post_only_and_require_csrf(self):
        self.assertEqual(self.client.get(self.add_url).status_code, 405)
        self.assertEqual(self.client.get(self.delete_url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.add_url, {'card': self.available.pk}).status_code, 403)
        self.assertEqual(client.post(self.delete_url).status_code, 403)
        self.assertEqual(self.user.user_cards.count(), 1)

    def test_detail_spending_and_monthly_usage_match_the_existing_estimator(self):
        self.payment(self.owned, 300_000, datetime(2026, 10, 31, 23, 59, tzinfo=SEOUL))
        for day in (1, 2, 3):
            self.payment(self.owned, 50_000, datetime(2026, 11, day, tzinfo=SEOUL))
        self.payment(self.owned, 90_000, AS_OF)
        other_link = UserCard.objects.create(user=self.other, card=self.product)
        self.payment(other_link, 50_000, AS_OF - timedelta(days=1))
        estimate = estimate_card_discount(user_id=self.user.pk, user_card_id=self.owned.pk,
                                          amount=50_000, category=Category.FOOD, as_of=AS_OF)
        with patch('cards.views.timezone.now', return_value=AS_OF):
            response = self.client.get(self.detail_url)
        self.assertContains(response, f'{estimate.previous_month_spending:,}원')
        self.assertContains(response, f'{estimate.current_month_spending:,}원')
        for label in ('음식점', '카페', '15% 할인', '20,000원', '3,000원', '10,000원', '조건 없음'):
            self.assertContains(response, label)
        self.assertEqual(estimate.estimated_monthly_discount_used, 9_000)
        self.assertEqual(estimate.remaining_monthly_limit, 1_000)
        food = next(benefit for benefit in response.context['benefits'] if benefit['category'] == '음식점')
        self.assertEqual(food['estimated_used'], '9,000원')
        self.assertEqual(food['remaining'], '1,000원')

    def test_detail_distinguishes_unlimited_exhausted_and_missing_benefits(self):
        self.payment(self.owned, 300_000, datetime(2026, 10, 20, tzinfo=SEOUL))
        for day in (1, 2, 3, 4):
            self.payment(self.owned, 50_000, datetime(2026, 11, day, tzinfo=SEOUL))
        no_benefit = UserCard.objects.create(user=self.user, card=self.available)
        with patch('cards.views.timezone.now', return_value=AS_OF):
            response = self.client.get(self.detail_url)
            empty = self.client.get(reverse('cards:card_detail', args=[no_benefit.pk]))
        status = {benefit['category']: benefit for benefit in response.context['benefits']}
        self.assertEqual(status['음식점']['remaining'], '0원')
        self.assertEqual(status['카페']['remaining'], '한도 없음')
        self.assertEqual(status['카페']['estimated_used'], '추정 대상 아님')
        self.assertContains(response, '한도 없음')
        self.assertContains(response, '추정 대상 아님')
        self.assertContains(empty, '등록된 혜택 없음')
