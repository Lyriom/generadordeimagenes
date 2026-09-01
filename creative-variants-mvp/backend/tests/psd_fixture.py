"""Generador de PSD multicapa para las pruebas.

`psd_tools.PSDImage.frompil()` escribe un PSD **sin capas** (solo la composición
plana), así que no sirve para probar la importación. Aquí se escribe un PSD mínimo
pero válido, con capas reales, nombres y canal alfa, siguiendo la especificación de
Adobe: cabecera → datos de color → recursos → capas y máscaras → imagen plana.

Todo va sin compresión (raw) para que el archivo sea trivial de generar y leer.
"""
from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image


def _pascal_string(value: str, padding: int = 4) -> bytes:
    """Cadena Pascal (1 byte de longitud) rellenada a múltiplo de `padding`."""
    raw = value.encode("latin-1", "replace")[:255]
    data = bytes([len(raw)]) + raw
    while len(data) % padding:
        data += b"\x00"
    return data


def _layer_record(layer: dict) -> bytes:
    """Registro de una capa: rectángulo, canales, mezcla y nombre."""
    image: Image.Image = layer["image"]
    left, top = layer["position"]
    right, bottom = left + image.width, top + image.height
    channel_bytes = image.width * image.height

    record = struct.pack(">iiii", top, left, bottom, right)
    record += struct.pack(">H", 4)  # canales: alfa + RGB
    for channel_id in (-1, 0, 1, 2):
        record += struct.pack(">h", channel_id)
        record += struct.pack(">I", channel_bytes + 2)  # +2 por el campo compresión

    record += b"8BIM" + b"norm"
    record += bytes([255, 0, 0, 0])  # opacidad, clipping, flags, relleno

    extra = struct.pack(">I", 0)  # datos de máscara de capa
    extra += struct.pack(">I", 0)  # rangos de mezcla
    extra += _pascal_string(layer["name"])
    record += struct.pack(">I", len(extra)) + extra
    return record


def _channel_data(layer: dict) -> bytes:
    """Datos de los canales de una capa, en orden alfa, R, G, B y sin comprimir."""
    rgba = layer["image"].convert("RGBA")
    red, green, blue, alpha = rgba.split()
    data = b""
    for channel in (alpha, red, green, blue):
        data += struct.pack(">H", 0)  # compresión: raw
        data += channel.tobytes()
    return data


def write_psd(path: Path, size: tuple[int, int], layers: list[dict]) -> Path:
    """Escribe un PSD con las capas indicadas.

    Cada capa es `{"name": str, "image": PIL.Image RGBA, "position": (x, y)}`.
    Las capas se listan de abajo hacia arriba, como en Photoshop.
    """
    width, height = size

    # --- composición plana (lo que abre cualquier visor) ---
    flat = Image.new("RGB", size, (255, 255, 255))
    for layer in layers:
        rgba = layer["image"].convert("RGBA")
        flat.paste(rgba, layer["position"], rgba)

    header = b"8BPS" + struct.pack(">H", 1) + b"\x00" * 6
    header += struct.pack(">H", 3)  # canales del documento (RGB)
    header += struct.pack(">II", height, width)
    header += struct.pack(">H", 8)  # bits por canal
    header += struct.pack(">H", 3)  # modo de color RGB

    color_mode_data = struct.pack(">I", 0)
    image_resources = struct.pack(">I", 0)

    records = b"".join(_layer_record(layer) for layer in layers)
    channels = b"".join(_channel_data(layer) for layer in layers)
    layer_info = struct.pack(">h", len(layers)) + records + channels
    if len(layer_info) % 2:
        layer_info += b"\x00"

    layer_and_mask = struct.pack(">I", len(layer_info)) + layer_info
    layer_and_mask += struct.pack(">I", 0)  # información global de máscara
    layer_and_mask = struct.pack(">I", len(layer_and_mask)) + layer_and_mask

    image_data = struct.pack(">H", 0)  # raw
    for channel in flat.split():
        image_data += channel.tobytes()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        header + color_mode_data + image_resources + layer_and_mask + image_data
    )
    return path


def solid(size: tuple[int, int], color: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", size, color)


def rounded(size: tuple[int, int], color: tuple[int, int, int, int]) -> Image.Image:
    """Forma con transparencia alrededor: sirve para comprobar que el alfa se respeta."""
    from PIL import ImageDraw

    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=12, fill=color)
    return image


def sample_kv(path: Path, width: int = 900, height: int = 660) -> Path:
    """KV de prueba parecido a los reales: fondo, logo, producto y legales."""
    layers = [
        {
            "name": "Relleno de color 1",
            "image": solid((width, height), (14, 30, 60, 255)),
            "position": (0, 0),
        },
        {
            "name": "LOGO MARATHON",
            "image": rounded((int(width * 0.28), int(height * 0.12)), (20, 90, 200, 255)),
            "position": (int(width * 0.05), int(height * 0.04)),
        },
        {
            "name": "Capa 5",
            "image": rounded((int(width * 0.34), int(height * 0.46)), (200, 40, 40, 255)),
            "position": (int(width * 0.55), int(height * 0.28)),
        },
        {
            "name": "legales",
            "image": rounded((int(width * 0.8), int(height * 0.05)), (240, 240, 240, 255)),
            "position": (int(width * 0.08), int(height * 0.9)),
        },
    ]
    return write_psd(path, (width, height), layers)


def sample_sheet(
    path: Path,
    pieces: list[tuple[int, int, int, int]],
    canvas: tuple[int, int],
) -> Path:
    """Pliego de prueba: varias piezas sobre un mismo lienzo, separadas por espacio.

    Reproduce los PSD que llegan de agencia con 4 avisos en un solo archivo. Como
    el escritor de PSD de las pruebas no sabe crear artboards, esto ejercita la
    detección geométrica, que es el camino de respaldo.
    """
    layers: list[dict] = []
    for index, (x, y, width, height) in enumerate(pieces):
        tone = 40 + index * 30
        layers.append(
            {
                "name": f"fondo pieza {index + 1}",
                "image": solid((width, height), (tone, 60, 90, 255)),
                "position": (x, y),
            }
        )
        layers.append(
            {
                "name": f"LOGO {index + 1}",
                "image": rounded(
                    (int(width * 0.3), int(height * 0.1)), (20, 90, 200, 255)
                ),
                "position": (x + int(width * 0.06), y + int(height * 0.05)),
            }
        )
        layers.append(
            {
                "name": f"Capa {index + 1}",
                "image": rounded(
                    (int(width * 0.4), int(height * 0.35)), (200, 40, 40, 255)
                ),
                "position": (x + int(width * 0.3), y + int(height * 0.4)),
            }
        )
    return write_psd(path, canvas, layers)
