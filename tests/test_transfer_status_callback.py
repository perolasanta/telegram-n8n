"""Status callback coverage for shared and restaurant delivery bot updates."""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

import bot as bot_module
import whatsapp as whatsapp_module


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
            return Result([{"id": "order-12345678", "restaurant_id": "restaurant-1"}])
        return Result([dict(self.database.payment)])


class FakeDB:
    def __init__(self, status):
        self.payment = {
            "order_id": "order-12345678", "restaurant_id": "restaurant-1",
            "status": status, "account_name": "Transfer Account",
            "account_number": "0123456789", "bank_name": "Test Bank",
            "amount": "250.00", "expires_at": "2099-01-01T00:30:00Z",
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
    async def test_status_replies_and_final_button_removal_for_both_bot_routes(self):
        expected = {
            "pending": "Payment is still pending",
            "confirmed": "Payment confirmed. Your order is being prepared.",
            "rejected": "rejected",
            "expired": "expired",
            "needs_review": "reviewing your payment",
            "cancelled": "cancelled",
            "refunded": "refund",
        }
        final = {"confirmed", "rejected", "expired", "cancelled"}
        for fixture in ("shared", "delivery"):
            for status, phrase in expected.items():
                with self.subTest(fixture=fixture, status=status):
                    callback = FakeCallback(fixture)
                    fake_db = FakeDB(status)
                    with patch.object(bot_module, "supabase", fake_db), \
                         patch("main.confirm_paystack_payment", AsyncMock(return_value={"status": status})) as verify:
                        await bot_module.check_instant_transfer(callback)
                    reply = callback.answer.await_args.args[0]
                    self.assertIn(phrase.lower(), reply.lower())
                    if status == "pending":
                        verify.assert_awaited_once()
                        self.assertIn("0123456789", reply)
                        self.assertIn("Time left", reply)
                    else:
                        verify.assert_not_awaited()
                    if status in final:
                        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
                    else:
                        callback.message.edit_reply_markup.assert_not_awaited()

    async def test_whatsapp_status_uses_stored_transfer_statuses(self):
        expected = {
            "pending": "Payment is still pending",
            "confirmed": "Payment confirmed. Your order is being prepared.",
            "rejected": "rejected",
            "expired": "expired",
            "needs_review": "reviewing your payment",
            "cancelled": "cancelled",
            "refunded": "refund",
        }

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
                    return Result([{"id": "order-12345678", "order_status": "pending",
                                    "payment_method": "Paystack", "payment_status": "pending",
                                    "total_amount": "250.00"}])
                row = dict(self.db.payment)
                row["paystack_reference"] = "order-12345678"
                return Result([row])

        class WhatsAppDB(FakeDB):
            def table(self, name):
                return WhatsAppQuery(self, name)

        restaurant = {"id": "restaurant-1", "whatsapp_phone_number_id": "phone-id",
                      "whatsapp_access_token": "token"}
        for status, phrase in expected.items():
            with self.subTest(status=status):
                fake_db = WhatsAppDB(status)
                with patch.object(whatsapp_module, "send_text", AsyncMock()) as send, \
                     patch("main.confirm_paystack_payment", AsyncMock(return_value={"status": status})) as verify:
                    await whatsapp_module.send_customer_order_status(
                        restaurant, "2348012345678", fake_db
                    )
                reply = send.await_args.args[-1]
                self.assertIn(phrase.lower(), reply.lower())
                if status == "pending":
                    verify.assert_awaited_once()
                    self.assertIn("0123456789", reply)
                    self.assertIn("Time left", reply)
                else:
                    verify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
