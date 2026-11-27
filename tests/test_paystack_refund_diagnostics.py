import os
import unittest
from unittest.mock import patch

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("PAYSTACK_SECRET_KEY", "sk_test_do_not_log_this")

import main


class Result:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.is_update = False

    def select(self, *_):
        return self

    def update(self, *_):
        self.is_update = True
        return self

    def eq(self, *_):
        return self

    def neq(self, *_):
        return self

    def execute(self):
        if self.is_update:
            return Result([{"order_id": "order-1"}])
        return Result([{
            "order_id": "order-1", "restaurant_id": "restaurant-1", "amount": "25.00",
            "status": "needs_review", "paystack_reference": "ref-1", "orders": {},
        }])


class FakeDB:
    def table(self, table):
        return Query(self, table)


class Response:
    status = 422

    async def json(self, **_):
        return {"status": False, "message": "Refund is not allowed", "code": "refund_not_allowed"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class Session:
    def post(self, *_args, **_kwargs):
        return Response()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class PaystackRefundDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_failure_logs_safe_paystack_reason(self):
        with patch.object(main, "supabase", FakeDB()), \
             patch.object(main.aiohttp, "ClientSession", return_value=Session()):
            with self.assertLogs(main.logger, "WARNING") as logs:
                result = await main.refund_paystack_payment("ref-1")
        text = "\n".join(logs.output)
        self.assertEqual(result, {"status": "failed"})
        self.assertIn("stage=api_error", text)
        self.assertIn("http_status=422", text)
        self.assertIn("Refund is not allowed", text)
        self.assertIn("refund_not_allowed", text)
        self.assertNotIn("sk_test_do_not_log_this", text)


if __name__ == "__main__":
    unittest.main()
