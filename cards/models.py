from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Category(models.TextChoices):
    CAFE = 'CAFE', '카페'
    FOOD = 'FOOD', '음식점'
    CONVENIENCE = 'CONVENIENCE', '편의점'
    MART = 'MART', '마트'
    DEPARTMENT_STORE = 'DEPARTMENT_STORE', '백화점'
    EDUCATION = 'EDUCATION', '교육'
    TRANSPORT = 'TRANSPORT', '교통'
    ONLINE = 'ONLINE', '온라인쇼핑'
    OTHER = 'OTHER', '기타'


class BenefitType(models.TextChoices):
    DISCOUNT = 'DISCOUNT', '할인'


class Card(models.Model):
    name = models.CharField(max_length=100)
    issuer = models.CharField(max_length=50)

    def __str__(self):
        return f'{self.issuer} - {self.name}'


class UserCard(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='user_cards',
    )
    card = models.ForeignKey(
        Card,
        on_delete=models.PROTECT,
        related_name='user_cards',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'card'], name='unique_user_card',
            ),
        ]

    def __str__(self):
        return f'{self.user_id} - {self.card}'


class CardBenefit(models.Model):
    card = models.ForeignKey(
        Card, on_delete=models.CASCADE, related_name='benefits',
    )
    category = models.CharField(max_length=30, choices=Category.choices)
    benefit_type = models.CharField(
        max_length=20, choices=BenefitType.choices,
        default=BenefitType.DISCOUNT,
    )
    benefit_rate = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[
            MinValueValidator(Decimal('0.01')),
            MaxValueValidator(Decimal('100.00')),
        ],
    )
    minimum_amount = models.PositiveIntegerField(default=0)
    required_spending = models.PositiveIntegerField(default=0)
    monthly_limit = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)],
        help_text='NULL은 혜택 조건을 확인한 결과 월 한도가 없음을 의미합니다.',
    )
    per_transaction_limit = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)],
        help_text='NULL은 혜택 조건을 확인한 결과 건별 한도가 없음을 의미합니다.',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['card', 'category', 'benefit_type'],
                name='unique_card_category_benefit',
            ),
            models.CheckConstraint(
                condition=models.Q(benefit_rate__gt=0, benefit_rate__lte=100),
                name='benefit_rate_valid_range',
            ),
            models.CheckConstraint(
                condition=(models.Q(monthly_limit__isnull=True)
                           | models.Q(monthly_limit__gt=0)),
                name='monthly_limit_null_or_positive',
            ),
            models.CheckConstraint(
                condition=(models.Q(per_transaction_limit__isnull=True)
                           | models.Q(per_transaction_limit__gt=0)),
                name='per_tx_limit_null_or_positive',
            ),
        ]

    def __str__(self):
        return f'{self.card} / {self.get_category_display()}'


class Transaction(models.Model):
    user_card = models.ForeignKey(
        UserCard, on_delete=models.PROTECT, related_name='transactions',
    )
    amount = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    category = models.CharField(max_length=30, choices=Category.choices)
    merchant_name = models.CharField(max_length=100)
    transaction_date = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='transaction_amount_positive',
            ),
        ]

    def __str__(self):
        return f'{self.merchant_name} / {self.amount}원'
