"""Payload-limit tests that do not make WhatsApp network requests."""

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from whatsapp import (
    build_buttons_payload, build_list_payload, build_catalog_payload,
    build_catalog_welcome_text, send_catalog_message, send_text,
    MAX_WHATSAPP_CATALOG_FOOTER_LENGTH,
)


class TestWhatsAppPayloadBuilders(unittest.TestCase):

    def test_catalog_welcome_text_adapts_to_pickup(self):
        pickup, footer = build_catalog_welcome_text("Chowlin", True)
        delivery, delivery_footer = build_catalog_welcome_text("Chowlin", False)
        self.assertIn("shop icon", pickup)
        self.assertIn("delivery", pickup)
        self.assertIn("pickup", pickup)
        self.assertIn("delivery address", delivery)
        self.assertNotIn("or *pickup*", delivery)
        self.assertEqual(footer, "Can't see it? Tap the shop icon at the top right.")
        self.assertEqual(delivery_footer, footer)
        self.assertLessEqual(len(footer), MAX_WHATSAPP_CATALOG_FOOTER_LENGTH)
        for name in ("A", "a" * 250, "餐厅" * 100):
            self.assertLessEqual(
                len(build_catalog_welcome_text(name, False)[1]),
                MAX_WHATSAPP_CATALOG_FOOTER_LENGTH,
            )
        self.assertLess(len(pickup), 1024)
        self.assertLess(len(delivery), 1024)

    def test_catalog_payload_shape(self):
        payload = build_catalog_payload("Welcome", "Footer")
        interactive = payload["interactive"]
        self.assertEqual(payload["type"], "interactive")
        self.assertEqual(interactive["type"], "catalog_message")
        self.assertEqual(interactive["action"], {"name": "catalog_message"})
        self.assertIn("footer", interactive)
        self.assertNotIn("thumbnail_product_retailer_id", interactive["action"])
        self.assertNotIn("parameters", interactive["action"])

    def test_catalog_payload_omits_overlong_footer(self):
        payload = build_catalog_payload("Welcome", "x" * (MAX_WHATSAPP_CATALOG_FOOTER_LENGTH + 1))
        self.assertNotIn("footer", payload["interactive"])

    def test_catalog_payload_strips_body_and_footer_whitespace(self):
        payload = build_catalog_payload("  Welcome  ", "  Footer  ")
        self.assertEqual(payload["interactive"]["body"]["text"], "Welcome")
        self.assertEqual(payload["interactive"]["footer"]["text"], "Footer")

    def test_list_body_label_title_and_description_are_truncated(self):
        payload = build_list_payload(
            "b" * 1100, "l" * 30,
            [{"id": "zone:1", "title": "t" * 40, "description": "d" * 100}],
        )
        interactive = payload["interactive"]
        row = interactive["action"]["sections"][0]["rows"][0]
        for value, limit in (
            (interactive["body"]["text"], 1024),
            (interactive["action"]["button"], 20),
            (row["title"], 24),
            (row["description"], 72),
        ):
            self.assertEqual(len(value), limit)
            self.assertTrue(value.endswith("…"))

    def test_button_body_and_title_are_truncated(self):
        payload = build_buttons_payload(
            "b" * 1100, [{"id": "confirm", "title": "t" * 30}],
        )
        self.assertEqual(len(payload["interactive"]["body"]["text"]), 1024)
        self.assertEqual(len(payload["interactive"]["action"]["buttons"][0]["reply"]["title"]), 20)

    def test_over_limit_ids_raise(self):
        with self.assertRaises(ValueError):
            build_list_payload("body", "Choose", [{"id": "x" * 201, "title": "row"}])
        with self.assertRaises(ValueError):
            build_buttons_payload("body", [{"id": "x" * 257, "title": "button"}])

    def test_empty_description_is_dropped(self):
        payload = build_list_payload("body", "Choose", [{"id": "row", "title": "Title", "description": "  "}])
        row = payload["interactive"]["action"]["sections"][0]["rows"][0]
        self.assertNotIn("description", row)

    def test_empty_title_raises(self):
        with self.assertRaises(ValueError):
            build_list_payload("body", "Choose", [{"id": "row", "title": "  "}])
        with self.assertRaises(ValueError):
            build_buttons_payload("body", [{"id": "reply", "title": "  "}])

    def test_row_and_button_count_limits(self):
        with self.assertRaises(ValueError):
            build_list_payload("body", "Choose", [{"id": str(i), "title": "row"} for i in range(11)])
        with self.assertRaises(ValueError):
            build_buttons_payload("body", [{"id": str(i), "title": "button"} for i in range(4)])

    def test_unicode_truncation_does_not_split_codepoints(self):
        result = build_list_payload("₦😀" * 600, "Choose", [{"id": "row", "title": "😀₦" * 20}])
        body = result["interactive"]["body"]["text"]
        title = result["interactive"]["action"]["sections"][0]["rows"][0]["title"]
        self.assertEqual(len(body), 1024)
        self.assertEqual(len(title), 24)
        self.assertTrue(body.endswith("…"))
        self.assertTrue(title.endswith("…"))


class TestSendTextLimit(unittest.IsolatedAsyncioTestCase):

    async def test_send_text_truncates_5000_characters(self):
        sender = AsyncMock()
        with patch("whatsapp.send_whatsapp_message", sender):
            await send_text("phone-id", "token", "+234", "x" * 5000)
        payload = sender.await_args.args[3]
        body = payload["text"]["body"]
        self.assertEqual(len(body), 4096)
        self.assertTrue(body.endswith("…"))

        class FakeSupabase:
            def __init__(self):
                self.table_calls = []

            def table(self, table_name):
                self.table_calls.append(table_name)
                raise AssertionError("catalog map query should not be made")

        calls = []

        async def sender(*args):
            calls.append(args[3])
            if len(calls) == 1:
                raise RuntimeError("catalog unavailable")
            return {"messages": [{"id": "fallback-1"}]}

        restaurant = {
            "id": "restaurant-1", "name": "Chowlin", "pickup_enabled": False,
            "whatsapp_phone_number_id": "phone-id", "whatsapp_access_token": "token",
        }
        supabase = FakeSupabase()
        with patch("whatsapp.send_whatsapp_message", sender):
            await send_catalog_message(restaurant, "+234", supabase)
        self.assertEqual(supabase.table_calls, [])
        self.assertEqual(calls[1]["type"], "text")
        self.assertEqual(
            calls[1]["text"]["body"],
            build_catalog_welcome_text("Chowlin", False)[0]
            + "\n\nCan't see it? Tap Chowlin at the top of this chat, then View catalog.",
        )
