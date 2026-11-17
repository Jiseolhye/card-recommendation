from django.contrib import admin

from .models import Card, CardBenefit, Transaction, UserCard


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'issuer')


@admin.register(UserCard)
class UserCardAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'card')


@admin.register(CardBenefit)
class CardBenefitAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'card', 'category', 'benefit_type', 'benefit_rate',
        'minimum_amount', 'required_spending', 'monthly_limit',
        'per_transaction_limit',
    )


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user_card', 'merchant_name', 'category', 'amount',
        'transaction_date',
    )
