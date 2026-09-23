"""Prepare notebook MIME images for stable, model-visible content blocks."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping

import resvg_py
from PIL import Image, ImageOps

_MIME_PRIORITY = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/svg+xml",
)
_MAXIMUM_SOURCE_BYTES = 20_000_000
_MAXIMUM_PIXELS = 32_000_000
_MAXIMUM_SIDE = 2048


@dataclass(frozen=True, slots=True)
class PreparedImage:
    mime_type: str
    base64_data: str
    source_sha256: str
    width: int
    height: int

    def descriptor(self) -> dict[str, str | int]:
        return {
            "mime_type": self.mime_type,
            "source_sha256": self.source_sha256,
            "width": self.width,
            "height": self.height,
        }

    def content_block(self) -> dict[str, str]:
        return {"type": "image", "mime_type": self.mime_type, "base64": self.base64_data}


def _mime_value(value: Any) -> str | bytes:
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return "".join(value)
    raise ValueError("Notebook image MIME data must be bytes or text")


def prepare_mime_image(bundle: Mapping[str, Any]) -> PreparedImage | None:
    """Choose one displayed rendition and return a validated raster image."""
    mime = next((candidate for candidate in _MIME_PRIORITY if candidate in bundle), None)
    if mime is None:
        if any(key.startswith("image/") for key in bundle):
            raise ValueError("Notebook image has no supported MIME rendition")
        return None

    value = _mime_value(bundle[mime])
    if mime == "image/svg+xml":
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        source = value.encode("utf-8")
        if len(source) > _MAXIMUM_SOURCE_BYTES:
            raise ValueError("Notebook SVG exceeds the image size limit")
        raster = resvg_py.svg_to_bytes(
            svg_string=value,
            width=_MAXIMUM_SIDE,
            height=_MAXIMUM_SIDE,
        )
        mime = "image/png"
    else:
        if isinstance(value, bytes):
            source = value
        else:
            if value.startswith("data:"):
                value = value.split(",", 1)[-1]
            if len(value) > _MAXIMUM_SOURCE_BYTES * 4 // 3 + 16:
                raise ValueError("Notebook image exceeds the image size limit")
            try:
                source = base64.b64decode(value, validate=True)
            except (ValueError, base64.binascii.Error) as error:
                raise ValueError("Notebook image contains invalid base64") from error
        raster = source

    if len(source) > _MAXIMUM_SOURCE_BYTES:
        raise ValueError("Notebook image exceeds the image size limit")
    source_sha256 = hashlib.sha256(source).hexdigest()

    try:
        with Image.open(BytesIO(raster)) as opened:
            width, height = opened.size
            if width * height > _MAXIMUM_PIXELS:
                raise ValueError("Notebook image exceeds the pixel limit")
            if width <= 0 or height <= 0:
                raise ValueError("Notebook image has invalid dimensions")
            opened.load()
            transpose = bool(opened.getexif().get(274, 1) != 1)
            convert = (
                transpose
                or max(width, height) > _MAXIMUM_SIDE
                or getattr(opened, "n_frames", 1) > 1
            )
            if convert:
                image = ImageOps.exif_transpose(opened)
                image.thumbnail((_MAXIMUM_SIDE, _MAXIMUM_SIDE), Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, format="PNG", optimize=True)
                raster = output.getvalue()
                mime = "image/png"
                width, height = image.size
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("Notebook output is not a readable image") from error

    return PreparedImage(
        mime_type=mime,
        base64_data=base64.b64encode(raster).decode("ascii"),
        source_sha256=source_sha256,
        width=width,
        height=height,
    )


__all__ = ["PreparedImage", "prepare_mime_image"]
