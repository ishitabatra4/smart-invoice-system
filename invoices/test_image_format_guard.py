"""Pillow advisory mitigation: only PNG/JPEG decoders are reachable from uploads."""
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from .file_security import validate_document


def encoded(fmt):
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format=fmt)
    return buffer.getvalue()


class ImageFormatGuardTests(SimpleTestCase):
    def test_png_and_jpeg_still_accepted(self):
        validate_document(SimpleUploadedFile("scan.png", encoded("PNG"), content_type="image/png"))
        validate_document(SimpleUploadedFile("scan.jpg", encoded("JPEG"), content_type="image/jpeg"))

    def test_other_pillow_formats_rejected_even_with_image_extension(self):
        for fmt in ("GIF", "TIFF", "BMP"):
            with self.subTest(fmt=fmt), self.assertRaises(ValidationError):
                validate_document(SimpleUploadedFile("scan.png", encoded(fmt), content_type="image/png"))
