import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction

from .models import Category, Transaction


CATEGORY_MAP = {
    "일반음식점": Category.FOOD,
    "커피전문점": Category.CAFE,
    "대형할인점": Category.MART,
}
CARD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class TransactionImportError(Exception):
    pass


def fetch_transactions(base_url, token, card_id):
    try:
        url = urlsplit(base_url)
        valid_url = (
            url.scheme == "http"
            and url.hostname == "127.0.0.1"
            and url.port is not None
            and url.netloc == f"127.0.0.1:{url.port}"
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise TransactionImportError("CARD_API_BASE_URL에 로컬 Mock 서버 주소를 설정하세요.")
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
        raise TransactionImportError("CARD_API_TOKEN에 유효한 테스트 토큰을 설정하세요.")
    if not CARD_ID_PATTERN.fullmatch(card_id):
        raise TransactionImportError("Mock 카드 식별자 형식이 올바르지 않습니다.")

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                f"{base_url.rstrip('/')}/cards/{card_id}/transactions",
                headers={"Authorization": f"Bearer {token}"},
                timeout=(2, 5),
                allow_redirects=False,
            )
    except requests.Timeout:
        raise TransactionImportError("Mock API 요청 시간이 초과되었습니다.") from None
    except requests.ConnectionError:
        raise TransactionImportError("Mock API에 연결할 수 없습니다.") from None
    except requests.RequestException:
        raise TransactionImportError("Mock API 요청에 실패했습니다.") from None

    if response.status_code == 401:
        raise TransactionImportError("Mock API 인증에 실패했습니다 (HTTP 401).")
    if response.status_code == 404:
        raise TransactionImportError("Mock 카드가 존재하지 않습니다 (HTTP 404).")
    if response.status_code != 200:
        raise TransactionImportError(f"Mock API가 HTTP {response.status_code}를 반환했습니다.")
    try:
        return response.json()
    except ValueError:
        raise TransactionImportError("Mock API 응답이 유효한 JSON이 아닙니다.") from None


def parse_timestamp(value):
    if not isinstance(value, str) or not TIMESTAMP_PATTERN.fullmatch(value):
        raise TransactionImportError("거래 시각은 오프셋이 있는 ISO 8601 형식이어야 합니다.")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        raise TransactionImportError("거래 시각이 유효하지 않습니다.") from None


def parse_transaction(row, user_card):
    if not isinstance(row, dict):
        raise TransactionImportError("거래는 JSON 객체여야 합니다.")
    amount = row.get("amount")
    if type(amount) is not int or amount < 1:
        raise TransactionImportError("결제금액은 양의 정수여야 합니다.")
    merchant_name = row.get("merchant_name")
    if not isinstance(merchant_name, str) or not 1 <= len(merchant_name.strip()) <= 100:
        raise TransactionImportError("가맹점명은 1~100자의 문자열이어야 합니다.")
    category = row.get("merchant_category")
    if not isinstance(category, str) or not category.strip():
        raise TransactionImportError("업종은 비어 있지 않은 문자열이어야 합니다.")
    return Transaction(
        user_card=user_card,
        amount=amount,
        merchant_name=merchant_name.strip(),
        category=CATEGORY_MAP.get(category.strip(), Category.OTHER),
        transaction_date=parse_timestamp(row.get("transaction_date")),
    )


def import_transactions(*, base_url, token, card_id, user_card):
    payload = fetch_transactions(base_url, token, card_id)
    if not isinstance(payload, dict):
        raise TransactionImportError("API 응답은 JSON 객체여야 합니다.")
    if payload.get("card_id") != card_id:
        raise TransactionImportError("요청과 응답의 카드 식별자가 일치하지 않습니다.")
    if payload.get("currency") != "KRW":
        raise TransactionImportError("원화(KRW) 거래만 저장할 수 있습니다.")
    rows = payload.get("transactions")
    if not isinstance(rows, list):
        raise TransactionImportError("거래 목록은 JSON 배열이어야 합니다.")

    payments = []
    try:
        with transaction.atomic():
            for index, row in enumerate(rows, start=1):
                try:
                    payment = parse_transaction(row, user_card)
                    payment.full_clean()
                except TransactionImportError as error:
                    raise TransactionImportError(f"거래 {index}번: {error}") from None
                except ValidationError:
                    raise TransactionImportError(f"거래 {index}번: 모델 검증에 실패했습니다.") from None
                payments.append(payment)

            for payment in payments:
                payment.save()
    except DatabaseError:
        raise TransactionImportError("DB 작업에 실패해 가져오기가 취소되었습니다.") from None

    return len(payments), sum(payment.category == Category.OTHER for payment in payments)
