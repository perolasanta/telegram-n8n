"""Focused transfer setup failure and success coverage."""
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

import whatsapp


DETAILS = {
    "reference": "order-ref-1", "account_number": "0123456789",
    "account_name": "Transfer Account", "bank_name": "Test Bank",
    "expires_at": "2099-01-01T00:30:00Z",
}


class InsertQuery:
    def __init__(self, fail=False):
        self.fail = fail
        self.values = None

    def insert(self, values):
        self.values = values
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError("database unavailable")
        return object()


class FakeDB:
    def __init__(self, fail=False):
        self.insert_query = InsertQuery(fail)

    def table(self, name):
        assert name == "payments"
        return self.insert_query


class FakeState:
    def __init__(self, db):
        self.supabase = db
        self.data = {"total_price": "125.50"}
        self.states = []

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return self.data

    async def set_state(self, value):
        self.states.append(value)


class InstantTransferSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.restaurant = {
            "id": "restaurant-1", "paystack_subaccount_code": "ACCT_1",
            "whatsapp_phone_number_id": "phone-id", "whatsapp_access_token": "token",
        }

    async def _run(self, db, charge_result):
        state = FakeState(db)
        order = {"id": "order-1", "total_amount": "125.50"}
        with patch.object(whatsapp, "create_order_in_db", AsyncMock(return_value=("order-1", order))), \
             patch.object(whatsapp, "create_transfer_charge", AsyncMock(return_value=charge_result)), \
             patch.object(whatsapp, "cancel_pending_order", AsyncMock()) as cancel, \
             patch.object(whatsapp, "send_text", AsyncMock()) as send:
            await whatsapp.start_instant_transfer(state, self.restaurant, "2348012345678", "Customer")
        return state, cancel, send

    async def test_persist_failure_cancels_and_sends_setup_message(self):
        _, cancel, send = await self._run(FakeDB(fail=True), DETAILS)
        cancel.assert_awaited_once()
        text = send.await_args.args[-1]
        self.assertIn("Something went wrong while setting up your transfer", text)
        self.assertNotIn("isn't available right now", text)

    async def test_charge_failure_sends_unavailable_message(self):
        _, cancel, send = await self._run(FakeDB(), None)
        cancel.assert_awaited_once()
        self.assertIn("Instant transfer isn't available right now", send.await_args.args[-1])

    async def test_success_persists_payment_and_sends_account_details(self):
        db = FakeDB()
        _, cancel, send = await self._run(db, DETAILS)
        cancel.assert_not_awaited()
        self.assertEqual(db.insert_query.values["restaurant_id"], "restaurant-1")
        self.assertEqual(db.insert_query.values["paystack_reference"], DETAILS["reference"])
        self.assertIn(DETAILS["account_number"], send.await_args.args[-1])


if __name__ == "__main__":
    unittest.main()
