"""Webhook fixture for a Paystack confirmation of a WhatsApp-created order."""

import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

# Importing bot creates an aiogram Bot, so provide harmless construction values
# when this test is run outside the deployed environment.
os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

import main


WHATSAPP_PAYSTACK_SUCCESS = {
    "event": "charge.success",
    "data": {"reference": "1f2d3c4b-0000-4000-8000-000000000001"},
}


class FakeRequest:
    def __init__(self, body: bytes, signature: str):
        self._body = body
        self.headers = {"x-paystack-signature": signature}

    async def body(self):
        return self._body


class Result:
    def __init__(self, data=None):
        self.data = data or []


class Query:
    def __init__(self, table_name, database):
        self.table_name = table_name
        self.database = database
        self.operation = "select"
        self.select_fields = ""

    def select(self, fields):
        self.select_fields = fields
        return self

    def update(self, values):
        self.operation = "update"
        self.database.updates.append((self.table_name, values))
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        if self.operation == "update":
            return Result()
        if self.table_name == "orders" and self.select_fields == "payment_status":
            return Result([{"payment_status": "pending"}])
        if self.table_name == "orders":
            return Result([{
                "telegram_user_id": None,
                "customer_contact": "2348012345678",
                "order_channel": "whatsapp",
                "restaurant_id": "restaurant-1",
                "restaurants": {
                    "kitchen_chat_id": -100123,
                    "whatsapp_phone_number_id": "phone-id",
                    "whatsapp_access_token": "token",
                },
            }])
        return Result()


class FakeSupabase:
    def __init__(self):
        self.updates = []

    def table(self, table_name):
        return Query(table_name, self)


class PaystackWhatsAppWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_whatsapp_order_confirmation_notifies_channel_and_sends_receipt(self):
        secret = "fixture-secret"
        body = json.dumps(WHATSAPP_PAYSTACK_SUCCESS).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
        fake_db = FakeSupabase()

        with patch.object(main, "PAYSTACK_SECRET_KEY", secret), \
             patch.object(main, "supabase", fake_db), \
             patch.object(main, "get_bot_for_order", AsyncMock(return_value=main.bot)), \
             patch.object(main, "send_order_to_kitchen_from_db", AsyncMock()) as kitchen, \
             patch.object(main, "deduct_inventory_for_order", AsyncMock(return_value=[])), \
             patch.object(main, "send_restock_alert", AsyncMock()) as restock, \
             patch.object(main, "notify_order_customer", AsyncMock()) as notify, \
             patch.object(main, "send_order_receipt", AsyncMock(return_value=True)) as receipt:
            result = await main.paystack_webhook(FakeRequest(body, signature))

        self.assertEqual(result, {"status": "ok"})
        self.assertTrue(any(table == "orders" and values == {"payment_status": "confirmed"} for table, values in fake_db.updates))
        kitchen.assert_awaited_once()
        restock.assert_awaited_once()
        notify.assert_awaited_once()
        receipt.assert_awaited_once()
        self.assertEqual(notify.await_args.args[1]["order_channel"], "whatsapp")


if __name__ == "__main__":
    unittest.main()
