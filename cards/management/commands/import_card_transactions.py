import os

from django.core.management.base import BaseCommand, CommandError

from cards.models import UserCard
from cards.transaction_import import TransactionImportError, import_transactions


class Command(BaseCommand):
    help = "로컬 Mock API에서 가상 카드 거래를 가져옵니다."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="로컬 가상 사용자의 이름")
        parser.add_argument("--user-card-id", required=True, type=int)
        parser.add_argument("--card-id", required=True, help="Mock API의 카드 식별자")

    def handle(self, *args, **options):
        try:
            user_card = UserCard.objects.get(
                pk=options["user_card_id"], user__username=options["username"],
            )
        except UserCard.DoesNotExist:
            raise CommandError("지정한 사용자에게 해당 UserCard가 없습니다.") from None

        try:
            count, other_count = import_transactions(
                base_url=os.getenv("CARD_API_BASE_URL", "").strip(),
                token=os.getenv("CARD_API_TOKEN", "").strip(),
                card_id=options["card_id"],
                user_card=user_card,
            )
        except TransactionImportError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(f"저장 {count}건 (OTHER {other_count}건)")
