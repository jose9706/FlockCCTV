"""Upside-down Unicode rendering for the optional message echo."""

from __future__ import annotations

import regex


_FLIP = dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    "ɐqɔpǝɟƃɥᴉɾʞןɯuodbɹsʇnʌʍxʎz",
))
_FLIP.update(zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "∀𐐒ƆᗡƎℲ⅁HIſꞰ˥WNOԀΌᴚS⊥∩ΛMX⅄Z",
))
_FLIP.update(zip("0123456789", "0ƖᄅƐㄣϛ9ㄥ86"))
_FLIP.update({
    "!": "¡", "?": "¿", ".": "˙", ",": "'", "'": ",", '"': "„",
    "(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{",
    "<": ">", ">": "<",
})


def upside_down(content: str) -> str:
    """Reverse grapheme clusters within each line and map visible characters."""
    lines = content.split("\n")
    return "\n".join(
        "".join("".join(_FLIP.get(char, char) for char in cluster)
                for cluster in reversed(regex.findall(r"\X", line)))
        for line in lines
    )


def evil_messages(content: str) -> list[str]:
    """Fit transformed text into Discord messages without splitting emoji."""
    if not content.strip():
        return []
    flipped = upside_down(content)
    chunks: list[str] = []
    current = "🙃 "
    units = len(current.encode("utf-16-le")) // 2
    for cluster in regex.findall(r"\X", flipped):
        size = len(cluster.encode("utf-16-le")) // 2
        if units + size > 1900:
            chunks.append(current)
            current = "🙃 "
            units = len(current.encode("utf-16-le")) // 2
        current += cluster
        units += size
    if current != "🙃 ":
        chunks.append(current)
    return chunks
