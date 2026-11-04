"""Gera um .ico com o logótipo do SafeOti (vários tamanhos, PNG embutido).

Uso: python -m installer.make_icon build\\SafeOti.ico
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def build_ico(path: str) -> None:
    from ui.icons import logo_pixmap

    images = []
    for size in SIZES:
        pm = logo_pixmap(size)  # renderizado a 2x
        img = pm.toImage().scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        images.append((size, bytes(data)))

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header + entries + blobs)


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    build_ico(sys.argv[1] if len(sys.argv) > 1 else os.path.join("build", "SafeOti.ico"))
