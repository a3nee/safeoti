"""Ícones de linha (SVG) desenhados em código, para um visual limpo e consistente."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ui import theme

# Caminhos SVG (viewBox 24x24, traço). Desenho próprio, estilo "line icons".
_PATHS = {
    "painel": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
              '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "limpeza": '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/>'
               '<path d="M18.5 15l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/>',
    "arranque": '<path d="M12 3v8"/><path d="M17.7 6.8a8 8 0 1 1-11.4 0"/>',
    "processos": '<path d="M22 12h-4l-3 8-6-16-3 8H2"/>',
    "gaming": '<path d="M7 7h10a5 5 0 0 1 4.9 6l-.7 3.4a2.6 2.6 0 0 1-4.4 1.3L14.5 15.5h-5l-2.3 2.2a2.6 2.6 0 0 1-4.4-1.3L2.1 13A5 5 0 0 1 7 7z"/>'
              '<path d="M7.5 10v4"/><path d="M5.5 12h4"/><path d="M15.5 11h.01"/><path d="M18 13h.01"/>',
    "definicoes": '<path d="M4 6h10"/><path d="M18 6h2"/><circle cx="16" cy="6" r="2"/>'
                  '<path d="M4 12h3"/><path d="M11 12h9"/><circle cx="9" cy="12" r="2"/>'
                  '<path d="M4 18h10"/><path d="M18 18h2"/><circle cx="16" cy="18" r="2"/>',
    "escudo": '<path d="M12 21.5s7.5-3.6 7.5-9.6V5.3L12 2.5 4.5 5.3v6.6c0 6 7.5 9.6 7.5 9.6z"/><path d="M8.8 11.8l2.2 2.2 4.2-4.3"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.5h.01"/>',
    "restauro": '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"/><path d="M3.5 3.5v5h5"/><path d="M12 8v4.5l3 1.8"/>',
    "cadeado": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    "cadeado_aberto": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 7.7-1.5"/>',
    "pasta": '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "atualizar": '<path d="M20 11a8 8 0 0 0-14.7-3.9L3.5 9"/><path d="M3.5 4v5h5"/><path d="M4 13a8 8 0 0 0 14.7 3.9l1.8-1.9"/><path d="M20.5 20v-5h-5"/>',
    "pesquisa": '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
    "disco": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 14.5h17"/><circle cx="16.5" cy="17" r=".6"/><path d="M7 17h5"/>',
    "programas": '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><path d="M16.75 13.5v6.5M13.5 16.75h6.5"/>',
    "saude": '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
    "bateria": '<rect x="3" y="7.5" width="16" height="9" rx="2"/><path d="M21 10.5v3"/><path d="M6.5 10.5v3M9.5 10.5v3"/>',
    "transferir": '<path d="M12 4v11"/><path d="M7.5 10.5L12 15l4.5-4.5"/><path d="M5 19.5h14"/>',
    "lixo": '<path d="M4.5 7h15"/><path d="M9.5 7V5h5v2"/><path d="M6.5 7l1 12.5h9l1-12.5"/>',
    "abrir": '<path d="M13.5 4.5h6v6"/><path d="M19.5 4.5l-8 8"/><path d="M18 14v4.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1H10"/>',
}

MUTED = "#6b6b6b"
INK = "#111111"
WHITE = "#ffffff"


def _svg(name: str, color: str, stroke: float = 1.8) -> bytes:
    body = _PATHS[name]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    ).encode("utf-8")


def pixmap(name: str, color: str = INK, size: int = 18, stroke: float = 1.8) -> QPixmap:
    """Ícone renderizado a 2x; a cor (escrita no tema claro) segue o tema ativo."""
    return _pixmap(name, theme.c(color), size, stroke)


@lru_cache(maxsize=256)
def _pixmap(name: str, color: str, size: int, stroke: float) -> QPixmap:
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    renderer = QSvgRenderer(QByteArray(_svg(name, color, stroke)))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
    painter.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name: str, color: str = INK, size: int = 18) -> QIcon:
    result = QIcon()
    result.addPixmap(pixmap(name, color, size), QIcon.Normal, QIcon.Off)
    result.addPixmap(pixmap(name, "#b5b5b5", size), QIcon.Disabled, QIcon.Off)
    return result


def nav_icon(name: str, size: int = 18) -> QIcon:
    """Ícone da barra lateral: cinzento normal, branco quando selecionado."""
    result = QIcon()
    result.addPixmap(pixmap(name, MUTED, size), QIcon.Normal, QIcon.Off)
    result.addPixmap(pixmap(name, INK, size), QIcon.Active, QIcon.Off)
    result.addPixmap(pixmap(name, WHITE, size), QIcon.Selected, QIcon.Off)
    result.addPixmap(pixmap(name, WHITE, size), QIcon.Selected, QIcon.On)
    return result


def logo_pixmap(size: int = 32) -> QPixmap:
    """Logótipo: quadrado preto arredondado com escudo branco."""
    scale = 2
    full = size * scale
    pm = QPixmap(full, full)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, full, full), full * 0.26, full * 0.26)
    painter.fillPath(path, QColor("#000000"))
    renderer = QSvgRenderer(QByteArray(_svg("escudo", WHITE, 2.0)))
    margin = full * 0.2
    renderer.render(painter, QRectF(margin, margin, full - 2 * margin, full - 2 * margin))
    painter.end()
    pm.setDevicePixelRatio(scale)
    return pm


def app_icon() -> QIcon:
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = logo_pixmap(size)
        pm.setDevicePixelRatio(1)
        result.addPixmap(pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return result
