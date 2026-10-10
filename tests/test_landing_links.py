import unittest
import os

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from main import (
    build_landing_links,
    format_display_number,
    readable_text_color,
    sanitize_brand_color,
    sanitize_logo_url,
)


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

    def test_sanitize_brand_color_accepts_hex_and_uses_neutral_fallback(self):
        self.assertEqual(sanitize_brand_color("#a1B2c3"), "#a1B2c3")
        for value in ("red", "#123", "#12345678", "#12GG56", None):
            with self.subTest(value=value):
                self.assertEqual(sanitize_brand_color(value), "#1F2937")

    def test_readable_text_color_uses_wcag_luminance(self):
        self.assertEqual(readable_text_color("#FFFFFF"), "#111111")
        self.assertEqual(readable_text_color("#000000"), "#FFFFFF")

    def test_sanitize_logo_url_requires_short_https_url(self):
        self.assertEqual(sanitize_logo_url("https://cdn.example/logo.png"), "https://cdn.example/logo.png")
        self.assertIsNone(sanitize_logo_url("http://cdn.example/logo.png"))
        self.assertIsNone(sanitize_logo_url("https://" + "a" * 493))

    def test_format_display_number_groups_nigerian_numbers(self):
        self.assertEqual(format_display_number("2348012345678"), "+234 801 234 5678")
        self.assertEqual(format_display_number("08012345678"), "+234 801 234 5678")
        self.assertEqual(format_display_number("14155552671"), "+14155552671")


if __name__ == "__main__":
    unittest.main()
