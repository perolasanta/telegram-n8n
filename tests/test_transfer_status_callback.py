"""Status callback coverage for shared and restaurant delivery bot updates."""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

import bot as bot_module
import whatsapp as whatsapp_module
from paystack_transfer import build_order_status_text


class Result:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, database, table):
        self.database = database
        self.table_name = table
        self.filters = {}

    def select(self, *_):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        if self.table_name == "orders":
            return Result([dict(self.database.order)])
        return Result([dict(self.database.payment)])


class FakeDB:
    def __init__(self, status, order_status="pending", order_type="delivery", dispatch_sent_at=None):
        self.payment = {
            "order_id": "order-12345678", "restaurant_id": "restaurant-1",
            "status": status, "account_name": "Transfer Account",
            "account_number": "0123456789", "bank_name": "Test Bank",
            "amount": "250.00", "expires_at": "2099-01-01T00:30:00Z",
        }
        self.order = {
            "id": "order-12345678", "restaurant_id": "restaurant-1", "telegram_user_id": 42,
            "order_channel": "telegram", "order_status": order_status, "order_type": order_type,
            "dispatch_sent_at": dispatch_sent_at, "payment_method": "Paystack",
            "payment_status": "confirmed" if status == "confirmed" else "pending",
        }

    def table(self, name):
        return Query(self, name)


class FakeMessage:
    def __init__(self):
        self.edit_reply_markup = AsyncMock()


class FakeCallback:
    def __init__(self, fixture):
        self.data = "pcheck_order-12345678"
        self.from_user = type("User", (), {"id": 42})()
        self.message = FakeMessage()
        self.answer = AsyncMock()
        self.fixture = fixture  # Shared and dedicated delivery bots use the same dispatcher.


class TransferStatusCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_status_uses_current_order_for_shared_and_delivery_bots(self):
        cases = {
            ("received", "delivery", None): "We've received your order",
            ("preparing", "delivery", None): "being prepared",
            ("ready", "delivery", None): "ready and will be sent out",
            ("ready", "pickup", None): "ready for pickup",
            ("ready", "delivery", "now"): "on its way",
            ("delivered", "delivery", None): "was delivered",
            ("cancelled", "delivery", None): "was cancelled",
        }
        for fixture in ("shared", "delivery"):
            for (order_status, order_type, dispatched), phrase in cases.items():
                with self.subTest(fixture=fixture, order_status=order_status, order_type=order_type):
                    callback = FakeCallback(fixture)
                    fake_db = FakeDB("confirmed", order_status, order_type, dispatched)
                    with patch.object(bot_module, "supabase", fake_db), \
                         patch("main.confirm_paystack_payment", AsyncMock()) as verify:
                        await bot_module.check_instant_transfer(callback)
                    reply = callback.answer.await_args.args[0]
                    self.assertIn(phrase.lower(), reply.lower())
                    verify.assert_not_awaited()
                    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

    async def test_whatsapp_status_uses_current_confirmed_order_status(self):

        class WhatsAppQuery:
            def __init__(self, db, table):
                self.db, self.table_name = db, table

            def select(self, *_):
                return self

            def eq(self, *_):
                return self

            def order(self, *_args, **_kwargs):
                return self

            def limit(self, *_):
                return self

            def execute(self):
                if self.table_name == "orders":
                    return Result([dict(self.db.order)])
                row = dict(self.db.payment)
                row["paystack_reference"] = "order-12345678"
                return Result([row])

        class WhatsAppDB(FakeDB):
            def table(self, name):
                return WhatsAppQuery(self, name)

        restaurant = {"id": "restaurant-1", "whatsapp_phone_number_id": "phone-id",
                      "whatsapp_access_token": "token"}
        cases = {
            ("received", "delivery", None): "We've received your order",
            ("preparing", "delivery", None): "being prepared",
            ("ready", "delivery", None): "ready and will be sent out",
            ("ready", "pickup", None): "ready for pickup",
            ("ready", "delivery", "now"): "on its way",
            ("delivered", "delivery", None): "was delivered",
            ("cancelled", "delivery", None): "was cancelled",
        }
        for (order_status, order_type, dispatched), phrase in cases.items():
            with self.subTest(order_status=order_status, order_type=order_type):
                fake_db = WhatsAppDB("confirmed", order_status, order_type, dispatched)
                with patch.object(whatsapp_module, "send_text", AsyncMock()) as send, \
                     patch("main.confirm_paystack_payment", AsyncMock()) as verify:
                    await whatsapp_module.send_customer_order_status(
                        restaurant, "2348012345678", fake_db
                    )
                reply = send.await_args.args[-1]
                self.assertIn(phrase.lower(), reply.lower())
                verify.assert_not_awaited()

    def test_callbacks_and_whatsapp_share_one_status_builder(self):
        self.assertIs(bot_module.build_order_status_text, build_order_status_text)
        self.assertIs(whatsapp_module.build_order_status_text, build_order_status_text)


if __name__ == "__main__":
    unittest.main()
