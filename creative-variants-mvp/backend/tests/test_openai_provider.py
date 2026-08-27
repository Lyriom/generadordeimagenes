"""Contrato local del proveedor OpenAI sin realizar llamadas ni consumir saldo."""
from __future__ import annotations

import io

from PIL import Image

from app.providers.openai_inpaint import OpenAIInpaintProvider, _openai_mask


def test_openai_provider_requires_key():
    assert OpenAIInpaintProvider(api_key="").available() is False
    assert OpenAIInpaintProvider(api_key="prueba-no-se-envia").available() is True


def test_openai_mask_makes_erase_region_transparent(tmp_path):
    source = Image.new("L", (4, 4), 0)
    source.putpixel((2, 1), 255)
    path = tmp_path / "mask.png"
    source.save(path)

    converted = Image.open(io.BytesIO(_openai_mask(str(path)))).convert("RGBA")
    assert converted.getpixel((0, 0))[3] == 255
    assert converted.getpixel((2, 1))[3] == 0
