from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .models import Card, CardBenefit


class SeedMvpCardsTests(TestCase):
    def run_seed(self):
        output = StringIO()
        call_command('seed_mvp_cards', stdout=output)
        return output.getvalue()

    def test_creates_five_cards_and_expected_benefits(self):
        output = self.run_seed()

        self.assertEqual(Card.objects.count(), 5)
        self.assertEqual(CardBenefit.objects.count(), 5)
        benefits = CardBenefit.objects.order_by('card__name').values_list(
            'card__issuer', 'card__name', 'category', 'benefit_type',
            'benefit_rate', 'minimum_amount', 'required_spending',
            'monthly_limit', 'per_transaction_limit',
        )
        self.assertEqual(list(benefits), [
            ('가상카드사 A', '테스트카드 A', 'FOOD', 'DISCOUNT', Decimal('5.00'), 0, 0, None, None),
            ('가상카드사 B', '테스트카드 B', 'FOOD', 'DISCOUNT', Decimal('8.00'), 0, 0, None, None),
            ('가상카드사 C', '테스트카드 C', 'FOOD', 'DISCOUNT', Decimal('10.00'), 0, 300000, None, None),
            ('가상카드사 D', '테스트카드 D', 'FOOD', 'DISCOUNT', Decimal('12.00'), 20000, 0, None, None),
            ('가상카드사 E', '테스트카드 E', 'FOOD', 'DISCOUNT', Decimal('15.00'), 0, 0, 10000, 3000),
        ])
        self.assertIn('Card 생성: 5', output)
        self.assertIn('CardBenefit 생성: 5', output)

    def test_second_run_reuses_existing_rows(self):
        self.run_seed()
        card_ids = list(Card.objects.order_by('pk').values_list('pk', flat=True))
        benefit_ids = list(CardBenefit.objects.order_by('pk').values_list('pk', flat=True))

        output = self.run_seed()

        self.assertEqual(list(Card.objects.order_by('pk').values_list('pk', flat=True)), card_ids)
        self.assertEqual(
            list(CardBenefit.objects.order_by('pk').values_list('pk', flat=True)), benefit_ids
        )
        self.assertIn('Card 생성: 0', output)
        self.assertIn('Card 재사용: 5', output)
        self.assertIn('CardBenefit 생성: 0', output)
        self.assertIn('CardBenefit 재사용: 5', output)

    def test_conflicting_benefit_is_preserved_and_earlier_rows_rolled_back(self):
        card = Card.objects.create(issuer='가상카드사 E', name='테스트카드 E')
        benefit = CardBenefit.objects.create(
            card=card, category='FOOD', benefit_type='DISCOUNT',
            benefit_rate=Decimal('7.00'), monthly_limit=10000,
        )
        output = StringIO()

        with self.assertRaises(CommandError) as error:
            call_command('seed_mvp_cards', stdout=output)

        self.assertIn('테스트카드 E / FOOD / DISCOUNT', str(error.exception))
        self.assertIn('benefit_rate: 기존 7.00, 기대 15.00', str(error.exception))
        self.assertIn('per_transaction_limit: 기존 NULL, 기대 3000', str(error.exception))
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(Card.objects.count(), 1)
        self.assertEqual(CardBenefit.objects.count(), 1)
        benefit.refresh_from_db()
        self.assertEqual(benefit.benefit_rate, Decimal('7.00'))
        self.assertIsNone(benefit.per_transaction_limit)

    def test_duplicate_cards_abort_without_selecting_either_card(self):
        first = Card.objects.create(issuer='가상카드사 E', name='테스트카드 E')
        second = Card.objects.create(issuer='가상카드사 E', name='테스트카드 E')

        with self.assertRaisesMessage(
            CommandError, '가상카드사 E - 테스트카드 E 카드가 2건 존재합니다.'
        ):
            self.run_seed()

        self.assertEqual(set(Card.objects.values_list('pk', flat=True)), {first.pk, second.pk})
        self.assertFalse(CardBenefit.objects.exists())
