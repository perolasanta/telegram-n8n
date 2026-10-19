"""Unit tests for bot.is_valid_proof_message (pure, no network).

Importing bot constructs an aiogram Bot and a Supabase client at module level,
so dummy env vars are set first (same pattern as tests/test_paystack_whatsapp.py).
The helper itself only inspects message attributes, so lightweight stubs are
used instead of full aiogram Message objects.
"""

import os
import unittest
from types import SimpleNamespace

# Neutralise bot.py's module-level Bot / Supabase construction
os.environ.setdefault("TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")

from bot import is_valid_proof_message


def _msg(photo=None, document=None, **extra):
    """Build a minimal message stub with the attributes the helper reads."""
    return SimpleNamespace(photo=photo, document=document, **extra)


def _doc(mime_type):
    return SimpleNamespace(mime_type=mime_type)


class TestIsValidProofMessage(unittest.TestCase):

    # --- accepted: photos --------------------------------------------------

    def test_photo_accepted(self):
        self.assertTrue(is_valid_proof_message(_msg(photo=[SimpleNamespace(file_id="p1")])))

    def test_photo_with_largest_size_marker_accepted(self):
        # aiogram fills message.photo as a tuple of sizes; any non-empty value is a photo.
        sizes = (SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big"))
        self.assertTrue(is_valid_proof_message(_msg(photo=sizes)))

    # --- accepted: image documents ----------------------------------------

    def test_image_png_document_accepted(self):
        self.assertTrue(is_valid_proof_message(_msg(document=_doc("image/png"))))

    def test_image_jpeg_document_accepted(self):
        self.assertTrue(is_valid_proof_message(_msg(document=_doc("image/jpeg"))))

    def test_image_webp_document_accepted(self):
        self.assertTrue(is_valid_proof_message(_msg(document=_doc("image/webp"))))

    # --- rejected: non-image documents ------------------------------------

    def test_pdf_document_rejected(self):
        self.assertFalse(is_valid_proof_message(_msg(document=_doc("application/pdf"))))

    def test_text_document_rejected(self):
        self.assertFalse(is_valid_proof_message(_msg(document=_doc("text/plain"))))

    def test_video_document_rejected(self):
        self.assertFalse(is_valid_proof_message(_msg(document=_doc("video/mp4"))))

    def test_document_with_none_mime_type_rejected(self):
        self.assertFalse(is_valid_proof_message(_msg(document=_doc(None))))

    # --- rejected: no media ----------------------------------------------

    def test_plain_text_rejected(self):
        self.assertFalse(is_valid_proof_message(_msg(photo=None, document=None, text="hello")))

    def test_empty_photo_rejected(self):
        # An empty photo collection must not count as a photo.
        self.assertFalse(is_valid_proof_message(_msg(photo=(), document=None)))

    def test_sticker_rejected(self):
        self.assertFalse(is_valid_proof_message(
            _msg(photo=None, document=None, sticker=SimpleNamespace(file_id="s1"))
        ))

    def test_voice_note_rejected(self):
        self.assertFalse(is_valid_proof_message(
            _msg(photo=None, document=None, voice=SimpleNamespace(file_id="v1"))
        ))

    def test_animation_rejected(self):
        self.assertFalse(is_valid_proof_message(
            _msg(photo=None, document=None, animation=SimpleNamespace(file_id="a1"))
        ))

    # --- combined / precedence --------------------------------------------

    def test_photo_takes_precedence_over_document(self):
        # A message should not carry both, but if it did, a photo is still valid.
        self.assertTrue(is_valid_proof_message(
            _msg(photo=[SimpleNamespace(file_id="p1")], document=_doc("application/pdf"))
        ))

    def test_document_mime_type_must_start_with_image_slash(self):
        # "imagefoo/png" must NOT match the image/ prefix check.
        self.assertFalse(is_valid_proof_message(_msg(document=_doc("imagefoo/png"))))
        self.assertFalse(is_valid_proof_message(_msg(document=_doc("application/image"))))


if __name__ == "__main__":
    unittest.main()
