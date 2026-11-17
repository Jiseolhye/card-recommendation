from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from .models import BenefitType, Card, CardBenefit, Category, Transaction, UserCard


class ModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='learner')
        cls.card = Card.objects.create(name='학습 카드', issuer='가상 카드사')
        cls.user_card = UserCard.objects.create(user=cls.user, card=cls.card)

    def benefit(self, **overrides):
        values = {
            'card': self.card,
            'category': Category.CAFE,
            'benefit_rate': Decimal('5.00'),
        }
        values.update(overrides)
        return CardBenefit(**values)

    def payment(self, **overrides):
        values = {
            'user_card': self.user_card,
            'amount': 10000,
            'category': Category.CAFE,
            'merchant_name': '가상 카페',
            'transaction_date': timezone.now(),
        }
        values.update(overrides)
        return Transaction(**values)

    def assert_rejected_by_validation_and_db(self, instance):
        with self.assertRaises(ValidationError):
            instance.full_clean()
        # save()가 검증을 생략해도 DB 제약이 데이터를 보호하는지 확인한다.
        with self.assertRaises(IntegrityError), transaction.atomic():
            instance.save()

    def test_automatic_primary_keys_are_big_auto_fields(self):
        for model in (Card, UserCard, CardBenefit, Transaction):
            with self.subTest(model=model.__name__):
                self.assertIsInstance(model._meta.pk, models.BigAutoField)
                self.assertTrue(model._meta.pk.auto_created)

    def test_categories_and_supported_benefit_type(self):
        self.assertEqual(Category.values, [
            'CAFE', 'FOOD', 'CONVENIENCE', 'MART', 'DEPARTMENT_STORE',
            'EDUCATION', 'TRANSPORT', 'ONLINE', 'OTHER',
        ])
        self.assertEqual(BenefitType.values, ['DISCOUNT'])
        for category in Category:
            self.benefit(category=category).full_clean()
            self.payment(category=category).full_clean()

    def test_benefit_defaults_and_unlimited_values(self):
        benefit = self.benefit()
        benefit.full_clean()
        benefit.save()
        benefit.refresh_from_db()
        self.assertEqual(benefit.benefit_type, BenefitType.DISCOUNT)
        self.assertEqual(benefit.minimum_amount, 0)
        self.assertEqual(benefit.required_spending, 0)
        self.assertIsNone(benefit.monthly_limit)
        self.assertIsNone(benefit.per_transaction_limit)

    def test_valid_rate_boundaries_and_positive_limits(self):
        for rate in ('0.01', '100.00'):
            with self.subTest(rate=rate):
                benefit = self.benefit(
                    benefit_rate=Decimal(rate), monthly_limit=1,
                    per_transaction_limit=1,
                )
                benefit.full_clean()
                benefit.save()
                benefit.delete()

    def test_invalid_benefit_numbers_rejected_by_validation_and_db(self):
        invalid_values = {
            'benefit_rate': [Decimal('-0.01'), Decimal('0'), Decimal('100.01')],
            'monthly_limit': [-1, 0],
            'per_transaction_limit': [-1, 0],
            'minimum_amount': [-1],
            'required_spending': [-1],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_rejected_by_validation_and_db(
                        self.benefit(**{field: value}),
                    )

    def test_transaction_amount_must_be_positive(self):
        for amount in (-1, 0):
            with self.subTest(amount=amount):
                self.assert_rejected_by_validation_and_db(self.payment(amount=amount))
        payment = self.payment(amount=1)
        payment.full_clean()
        payment.save()

    def test_user_card_pair_must_be_unique(self):
        self.assert_rejected_by_validation_and_db(
            UserCard(user=self.user, card=self.card),
        )
        other_user = get_user_model().objects.create_user(username='other')
        UserCard.objects.create(user=other_user, card=self.card)
        other_card = Card.objects.create(name='다른 카드', issuer='가상 카드사')
        UserCard.objects.create(user=self.user, card=other_card)

    def test_benefit_combination_must_be_unique(self):
        self.benefit().save()
        self.assert_rejected_by_validation_and_db(self.benefit())
        self.benefit(category=Category.MART).save()
        other_card = Card.objects.create(name='다른 카드', issuer='가상 카드사')
        self.benefit(card=other_card).save()

    def test_invalid_choices_fail_validation(self):
        for instance in (
            self.benefit(category='SHOPPING'),
            self.benefit(benefit_type='POINT'),
            self.payment(category='UNKNOWN'),
        ):
            with self.subTest(model=type(instance).__name__):
                with self.assertRaises(ValidationError):
                    instance.full_clean()

    def test_required_fields_fail_validation(self):
        for instance in (
            Card(name='', issuer='가상 카드사'),
            Card(name='학습 카드', issuer=''),
            UserCard(card=self.card),
            UserCard(user=self.user),
            self.benefit(card=None),
            self.benefit(benefit_rate=None),
            self.payment(user_card=None),
            self.payment(merchant_name=''),
            self.payment(transaction_date=None),
        ):
            with self.subTest(model=type(instance).__name__):
                with self.assertRaises(ValidationError):
                    instance.full_clean()

    def test_transaction_datetime_and_relationship_round_trip(self):
        payment = self.payment()
        payment.full_clean()
        payment.save()
        saved = Transaction.objects.get(pk=payment.pk)
        self.assertEqual(saved.transaction_date, payment.transaction_date)
        self.assertTrue(timezone.is_aware(saved.transaction_date))
        self.assertEqual(saved.user_card.user, self.user)
        self.assertEqual(saved.user_card.card, self.card)
        fields = {field.name for field in Transaction._meta.fields}
        self.assertNotIn('user', fields)
        self.assertNotIn('card', fields)

    def test_user_deletion_cascades_without_transactions(self):
        user_card_pk = self.user_card.pk
        self.user.delete()
        self.assertFalse(UserCard.objects.filter(pk=user_card_pk).exists())
        self.assertTrue(Card.objects.filter(pk=self.card.pk).exists())

    def test_card_deletion_protected_by_user_card(self):
        with self.assertRaises(ProtectedError):
            self.card.delete()

    def test_card_deletion_cascades_to_benefits_when_not_owned(self):
        card = Card.objects.create(name='미보유 카드', issuer='가상 카드사')
        benefit = self.benefit(card=card)
        benefit.save()
        benefit_pk = benefit.pk
        card.delete()
        self.assertFalse(CardBenefit.objects.filter(pk=benefit_pk).exists())

    def test_transaction_protects_user_card_and_user(self):
        payment = self.payment()
        payment.save()
        with self.assertRaises(ProtectedError):
            self.user_card.delete()
        with self.assertRaises(ProtectedError):
            self.user.delete()
        self.assertTrue(Transaction.objects.filter(pk=payment.pk).exists())
