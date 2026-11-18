from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cards.models import BenefitType, Card, CardBenefit, Category


# 카드 구분, 할인율, 최소 결제금액, 전월실적, 월 한도, 건별 한도
MVP_CARDS = (
    ('A', '5.00', 0, 0, None, None),
    ('B', '8.00', 0, 0, None, None),
    ('C', '10.00', 0, 300000, None, None),
    ('D', '12.00', 20000, 0, None, None),
    ('E', '15.00', 0, 0, 10000, 3000),
)


class Command(BaseCommand):
    help = 'MVP 검증용 가상 카드와 음식점 할인 혜택을 생성합니다.'

    def handle(self, *args, **options):
        messages = []
        cards_created = 0
        benefits_created = 0

        with transaction.atomic():
            for suffix, rate, minimum, spending, monthly, per_transaction in MVP_CARDS:
                issuer = f'가상카드사 {suffix}'
                name = f'테스트카드 {suffix}'
                matches = Card.objects.filter(issuer=issuer, name=name)
                match_count = matches.count()
                if match_count > 1:
                    raise CommandError(
                        f'[ERROR] {issuer} - {name} 카드가 {match_count}건 존재합니다. '
                        'seed 작업을 중단합니다.'
                    )

                if match_count == 0:
                    card = Card.objects.create(issuer=issuer, name=name)
                    cards_created += 1
                    messages.append(f'[CREATE] {card}')
                else:
                    card = matches.get()
                    messages.append(f'[SKIP] {card}')

                expected = {
                    'benefit_rate': Decimal(rate),
                    'minimum_amount': minimum,
                    'required_spending': spending,
                    'monthly_limit': monthly,
                    'per_transaction_limit': per_transaction,
                }
                benefit, created = CardBenefit.objects.get_or_create(
                    card=card,
                    category=Category.FOOD,
                    benefit_type=BenefitType.DISCOUNT,
                    defaults=expected,
                )
                label = f'{name} / {Category.FOOD} / {BenefitType.DISCOUNT}'
                if created:
                    benefits_created += 1
                    messages.append(f'[CREATE] {label}')
                    continue

                differences = []
                for field, expected_value in expected.items():
                    actual_value = getattr(benefit, field)
                    if actual_value != expected_value:
                        actual_text = 'NULL' if actual_value is None else str(actual_value)
                        expected_text = 'NULL' if expected_value is None else str(expected_value)
                        differences.append(
                            f'{field}: 기존 {actual_text}, 기대 {expected_text}'
                        )
                if differences:
                    raise CommandError(
                        f'[ERROR] {label} 혜택 값이 기대값과 다릅니다. '
                        + ', '.join(differences)
                        + '. seed 작업을 중단합니다.'
                    )
                messages.append(f'[SKIP] {label}')

        for message in messages:
            self.stdout.write(message)
        self.stdout.write('\nMVP 카드 데이터 생성 완료')
        self.stdout.write(f'Card 생성: {cards_created}')
        self.stdout.write(f'Card 재사용: {len(MVP_CARDS) - cards_created}')
        self.stdout.write(f'CardBenefit 생성: {benefits_created}')
        self.stdout.write(f'CardBenefit 재사용: {len(MVP_CARDS) - benefits_created}')
