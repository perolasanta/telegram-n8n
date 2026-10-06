import unittest
import os

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from main import build_landing_links


class LandingLinksTests(unittest.TestCase):

    def test_strips_non_digits_from_display_number(self):
        self.assertEqual(
            build_landing_links("4", "table_4", "+234 (80) 1234-5678"),
            "https://wa.me/2348012345678?text=Table%204%20ref%3Atable_4",
        )

    def test_external_table_uses_greeting_copy_and_quotes_text(self):
        self.assertEqual(
            build_landing_links("EXTERNAL", "pickup-1", "2348012345678"),
            "https://wa.me/2348012345678?text=Hi%20ref%3Apickup-1",
        )
        self.assertEqual(
            build_landing_links(None, "pickup_2", "2348012345678"),
            "https://wa.me/2348012345678?text=Hi%20ref%3Apickup_2",
        )

    def test_rejects_numbers_shorter_than_8_or_longer_than_15_digits(self):
        self.assertIsNone(build_landing_links("1", "table_1", "123-4567"))
        self.assertIsNone(build_landing_links("1", "table_1", "+1234567890123456"))


if __name__ == "__main__":
    unittest.main()
