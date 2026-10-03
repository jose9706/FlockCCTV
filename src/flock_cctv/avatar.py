"""Small, deterministic avatar image transformation."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps


def invert_avatar(data: bytes) -> bytes:
    """Invert RGB colours, preserving transparency, and return a square PNG."""
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Avatar image is too large")
    with Image.open(BytesIO(data)) as source:
        source.seek(0)  # Animated avatars use their first frame.
        frame = ImageOps.exif_transpose(source).convert("RGBA")
        frame = ImageOps.fit(frame, (256, 256), method=Image.Resampling.LANCZOS)
        red, green, blue, alpha = frame.split()
        inverted = Image.merge("RGB", (red, green, blue))
        inverted = ImageOps.invert(inverted).convert("RGBA")
        inverted.putalpha(alpha)
        output = BytesIO()
        inverted.save(output, format="PNG", optimize=True)
        return output.getvalue()
