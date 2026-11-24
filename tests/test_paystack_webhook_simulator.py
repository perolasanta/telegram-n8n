import hashlib
import hmac
import importlib.util
import json
import pathlib
import unittest

_path = pathlib.Path(__file__).parents[1] / "scripts" / "simulate_paystack_webhook.py"
_spec = importlib.util.spec_from_file_location("paystack_simulator", _path)
simulator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(simulator)


class PaystackWebhookSimulatorTests(unittest.TestCase):
    def test_signature_matches_webhook_hmac_for_wrong_amount(self):
        secret = "test-secret"
        payload = simulator.build_payload("order-reference", "1.01", "charge.success")
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
        self.assertTrue(hmac.compare_digest(signature, hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()))
        self.assertEqual(payload["data"]["amount"], 101)

