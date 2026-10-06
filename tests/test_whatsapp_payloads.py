"""Payload-limit tests that do not make WhatsApp network requests."""

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from whatsapp import build_buttons_payload, build_list_payload, send_text


class TestWhatsAppPayloadBuilders(unittest.TestCase):

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
