"""Pure Charge payload fixtures; these run without a database."""
import os
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
os.environ.setdefault("PAYSTACK_COMMISSION_PERCENTAGE", "5")
from paystack_transfer import amount_to_kobo, build_transfer_charge_payload, transfer_expiry, transfer_customer_wording

class PaystackTransferHelperTests(unittest.TestCase):
    def test_decimal_kobo_has_no_float_error(self):
        self.assertEqual(amount_to_kobo("0.29"), 29)
        self.assertEqual(amount_to_kobo("1234.567"), 123457)
    def test_expiry_is_bounded(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(transfer_expiry(30, now), "2026-01-01T00:30:00Z")
        with self.assertRaises(ValueError): transfer_expiry(14, now)
    def test_payload_has_transfer_subaccount_and_split(self):
        with patch.dict(os.environ, {"PAYSTACK_COMMISSION_PERCENTAGE": "5"}):
            p = build_transfer_charge_payload({"id":"order-1","total_amount":"1000","restaurant_id":"r","order_channel":"whatsapp"}, {"id":"r","paystack_subaccount_code":"ACCT_x"}, "234@example.ng")
        self.assertEqual(p["amount"], 100000); self.assertEqual(p["subaccount"], "ACCT_x")
        self.assertIn("bank_transfer", p); self.assertEqual(p["transaction_charge"], 5000)
    def test_channel_wording_does_not_leak_commands(self):
        telegram = transfer_customer_wording("telegram")
        whatsapp = transfer_customer_wording("whatsapp")
        self.assertNotIn("STATUS", telegram); self.assertNotIn("MENU", telegram)
        self.assertNotIn("/status", whatsapp); self.assertNotIn("Check payment", whatsapp)

if __name__ == "__main__": unittest.main()
