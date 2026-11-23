import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv


CARD_PATH = "/cards/MOCK-CARD-A/transactions"
PAYLOAD = {
    "card_id": "MOCK-CARD-A",
    "currency": "KRW",
    "transactions": [
        {
            "amount": 23000,
            "merchant_name": "가상 식당",
            "merchant_category": "일반음식점",
            "transaction_date": "2026-11-01T00:10:00+09:00",
        },
        {
            "amount": 4500,
            "merchant_name": "가상 커피점",
            "merchant_category": "커피전문점",
            "transaction_date": "2026-11-01T09:20:00+09:00",
        },
    ],
}


class MockCardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        expected = f"Bearer {self.server.api_token}".encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            self.send_json(401, {"error": "unauthorized"})
        elif self.path != CARD_PATH:
            self.send_json(404, {"error": "card_not_found"})
        else:
            self.send_json(200, PAYLOAD)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # 기본 요청 로그에는 클라이언트가 보낸 문자열이 포함될 수 있다.
        pass


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    token = os.getenv("CARD_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("CARD_API_TOKEN을 설정하세요.")
    with HTTPServer(("127.0.0.1", 8765), MockCardHandler) as server:
        server.api_token = token
        print("Mock API 실행 중 (127.0.0.1:8765)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
