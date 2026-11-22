"""Pure tests for composite cart line construction."""

import os
import re
import unittest

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from bot import build_composite_cart_line, compute_composite_totals


class TestCompositeLine(unittest.TestCase):

    def test_single_pick(self):
        total, parts = compute_composite_totals(
            1000, {"protein": [{"name": "Chicken", "price_delta": 250, "quantity": 1}]}
        )
        self.assertEqual(total, 1250.0)
        self.assertEqual(parts, ["Chicken"])

    def test_multiple_picks_in_multiple_groups(self):
        total, parts = compute_composite_totals(
            1000,
            {
                "protein": [{"name": "Chicken", "price_delta": 250, "quantity": 1}],
                "sides": [
                    {"name": "Plantain", "price_delta": 100, "quantity": 1},
                    {"name": "Salad", "price_delta": 50, "quantity": 1},
                ],
            },
        )
        self.assertEqual(total, 1400.0)
        self.assertEqual(parts, ["Chicken", "Plantain, Salad"])

    def test_quantity_above_one_appears_in_name_and_total(self):
        total, parts = compute_composite_totals(
            500, {"sides": [{"name": "Samosa", "price_delta": 75, "quantity": 3}]}
        )
        self.assertEqual(total, 725.0)
        self.assertEqual(parts, ["Samosa x3"])

    def test_no_selections(self):
        total, parts = compute_composite_totals(1200, {})
        self.assertEqual(total, 1200.0)
        self.assertEqual(parts, [])

    def test_cart_key_format_and_uniqueness(self):
        item = {"id": "combo-1", "name": "Lunch Combo", "price": "1000.00"}
        first_key, line, summary = build_composite_cart_line(item, {})
        second_key, _, _ = build_composite_cart_line(item, {})

        self.assertRegex(first_key, re.compile(r"^combo-1_[0-9a-f]{8}$"))
        self.assertRegex(second_key, re.compile(r"^combo-1_[0-9a-f]{8}$"))
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(line, {
            "name": "Lunch Combo",
            "price": 1000.0,
            "qty": 1,
            "modifiers": {},
            "menu_item_id": "combo-1",
        })
        self.assertEqual(summary, "Lunch Combo —  — ₦1,000")


if __name__ == "__main__":
    unittest.main()
