from io import BytesIO
from PIL import Image


def valid_pdf():
    output = BytesIO()
    Image.new("RGB", (100, 100), "white").save(output, format="PDF")
    return output.getvalue()
