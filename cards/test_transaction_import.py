import os
from copy import deepcopy
from datetime import UTC, datetime
from http.server import HTTPServer
from io import StringIO
from threading import Thread
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase

from scripts.mock_card_api import PAYLOAD, MockCardHandler

from .models import Card, Category, Transaction, UserCard
from .transaction_import import TransactionImportError, import_transactions


class TransactionImportTests(TestCase):
    token = "mock-test-token"
    card_id = "MOCK-CARD-A"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        server = HTTPServer(("127.0.0.1", 0), MockCardHandler)
        cls.addClassCleanup(server.server_close)
        server.api_token = cls.token
        cls.base_url = f"http://127.0.0.1:{server.server_port}"
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        cls.addClassCleanup(worker.join)
        cls.addClassCleanup(server.shutdown)

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="mock-learner")
        card = Card.objects.create(name="가상 카드", issuer="가상 카드사")
        cls.user_card = UserCard.objects.create(user=cls.user, card=card)

    def run_import(self, **overrides):
        options = {
            "base_url": self.base_url, "token": self.token,
            "card_id": self.card_id, "user_card": self.user_card,
        }
        options.update(overrides)
        return import_transactions(**options)

    def test_command_requests_mock_and_saves_transactions(self):
        output = StringIO()
        with patch.dict(os.environ, CARD_API_BASE_URL=self.base_url, CARD_API_TOKEN=self.token):
            call_command(
                "import_card_transactions", username=self.user.username,
                user_card_id=self.user_card.pk, card_id=self.card_id, stdout=output,
            )
        self.assertIn("저장 2건 (OTHER 0건)", output.getvalue())
        self.assertNotIn(self.token, output.getvalue())
        self.assertEqual(Transaction.objects.count(), 2)
        payment = Transaction.objects.get(category=Category.FOOD)
        self.assertEqual(payment.user_card, self.user_card)
        self.assertEqual(payment.amount, 23000)
        self.assertEqual(payment.merchant_name, "가상 식당")
        self.assertEqual(payment.transaction_date, datetime(2026, 10, 31, 15, 10, tzinfo=UTC))
        self.assertTrue(Transaction.objects.filter(category=Category.CAFE).exists())

    def test_wrong_owner_is_rejected_before_request(self):
        with patch("requests.Session.get") as get:
            with self.assertRaisesMessage(CommandError, "해당 UserCard가 없습니다"):
                call_command(
                    "import_card_transactions", username="different-user",
                    user_card_id=self.user_card.pk, card_id=self.card_id,
                )
        get.assert_not_called()

    def test_authentication_failure(self):
        with self.assertRaisesMessage(TransactionImportError, "HTTP 401") as error:
            self.run_import(token="wrong-test-token")
        self.assertNotIn("wrong-test-token", str(error.exception))
        self.assertFalse(Transaction.objects.exists())

    def test_unknown_card(self):
        with self.assertRaisesMessage(TransactionImportError, "HTTP 404"):
            self.run_import(card_id="UNKNOWN")
        self.assertFalse(Transaction.objects.exists())

    def test_connection_failure_and_timeouts(self):
        for error, message in (
            (requests.ConnectionError, "연결할 수 없습니다"),
            (requests.ConnectTimeout, "시간이 초과"),
            (requests.ReadTimeout, "시간이 초과"),
        ):
            with self.subTest(error=error):
                with patch("requests.Session.get", side_effect=error) as get:
                    with self.assertRaisesMessage(TransactionImportError, message):
                        self.run_import()
                self.assertEqual(get.call_args.kwargs["timeout"], (2, 5))
        self.assertFalse(Transaction.objects.exists())

    def test_invalid_json(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b"{"
        with patch("requests.Session.get", return_value=response):
            with self.assertRaisesMessage(TransactionImportError, "유효한 JSON"):
                self.run_import()
        self.assertFalse(Transaction.objects.exists())

    def test_invalid_fields(self):
        for field, invalid_values in (
            ("amount", (None, True, "100", 1.5, 0, -1, 2**63)),
            ("merchant_name", (None, 8, " ", "a" * 101)),
            ("merchant_category", (None, [], " ")),
            ("transaction_date", (
                None, 123, "2026-11-01", "2026-11-01T00:10:00",
                "2026-11-01T00:10:00+09:60", "2026-02-30T00:10:00Z",
            )),
        ):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    payload = deepcopy(PAYLOAD)
                    row = payload["transactions"][1]
                    if value is None:
                        del row[field]
                    else:
                        row[field] = value
                    with patch("cards.transaction_import.fetch_transactions", return_value=payload):
                        with self.assertRaisesMessage(TransactionImportError, "거래 2번"):
                            self.run_import()
                    self.assertFalse(Transaction.objects.exists())

    def test_invalid_response_shape_currency_and_card_id(self):
        for payload, message in (
            ([], "JSON 객체"),
            ({**PAYLOAD, "card_id": "MOCK-CARD-B"}, "카드 식별자"),
            ({**PAYLOAD, "card_id": 1}, "카드 식별자"),
            ({**PAYLOAD, "currency": "USD"}, "원화"),
            ({**PAYLOAD, "transactions": {}}, "JSON 배열"),
        ):
            with self.subTest(message=message):
                with patch("cards.transaction_import.fetch_transactions", return_value=payload):
                    with self.assertRaisesMessage(TransactionImportError, message):
                        self.run_import()
                self.assertFalse(Transaction.objects.exists())

    def test_unmapped_category_and_utc_suffix(self):
        payload = deepcopy(PAYLOAD)
        payload["transactions"][0].update(
            merchant_category="미등록 업종", transaction_date="2026-10-31T15:10:00Z",
        )
        with patch("cards.transaction_import.fetch_transactions", return_value=payload):
            self.assertEqual(self.run_import(), (2, 1))
        payment = Transaction.objects.get(category=Category.OTHER)
        self.assertEqual(payment.transaction_date, datetime(2026, 10, 31, 15, 10, tzinfo=UTC))

    def test_database_failure_rolls_back_earlier_save(self):
        original_save = Transaction.save

        def fail_second(payment):
            if payment.category == Category.CAFE:
                self.assertEqual(Transaction.objects.count(), 1)
                raise IntegrityError("simulated database failure")
            original_save(payment)

        with patch.object(Transaction, "save", autospec=True, side_effect=fail_second):
            with self.assertRaisesMessage(TransactionImportError, "가져오기가 취소"):
                self.run_import()
        self.assertFalse(Transaction.objects.exists())
