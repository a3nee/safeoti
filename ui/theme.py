"""Tema claro/escuro.

O tema claro é a referência: ``styles.qss`` e o código usam as cores claras.
O tema escuro é obtido trocando cada cor clara pela equivalente escura
(``DARK``), por isso basta escrever ``theme.c("#111111")`` no código e as
folhas de estilo continuam escritas só uma vez.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Dict

log = logging.getLogger("safeoti.theme")

# Cor clara -> cor escura. Cinzentos invertidos (o preto passa a quase branco,
# o branco a cinzento muito escuro) e o vermelho de perigo um pouco mais claro.
DARK: Dict[str, str] = {
    "#ffffff": "#1c1c1c",
    "#fafafa": "#161616",
    "#f5f5f5": "#262626",
    "#f3f3f3": "#282828",
    "#f0f0f0": "#2a2a2a",
    "#efefef": "#2b2b2b",
    "#eeeeee": "#2e2e2e",
    "#ececec": "#323232",
    "#e3e3e3": "#3a3a3a",
    "#dedede": "#404040",
    "#dddddd": "#404040",
    "#dbdbdb": "#424242",
    "#c9c9c9": "#555555",
    "#bdbdbd": "#5e5e5e",
    "#b5b5b5": "#686868",
    "#b0b0b0": "#6e6e6e",
    "#a3a3a3": "#7a7a7a",
    "#9a9a9a": "#858585",
    "#8a8a8a": "#9a9a9a",
    "#6b6b6b": "#a8a8a8",
    "#5c5c5c": "#b4b4b4",
    "#3d3d3d": "#cfcfcf",
    "#333333": "#d6d6d6",
    "#111111": "#ececec",
    "#000000": "#ffffff",
    "#c62828": "#ef5350",
    "#fdf3f3": "#3a2222",
    "#f0caca": "#6b3434",
    "#2e7d32": "#66bb6a",
    "#e8f5e9": "#1f3321",
    "#b26a00": "#ffb74d",
    "#fff8e1": "#3a3120",
}

_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
_dark = False


def resolve(choice: str) -> bool:
    """Converte a escolha das definições ("sistema", "claro", "escuro") em escuro sim/não."""
    if choice == "escuro":
        return True
    if choice == "claro":
        return False
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # noqa: BLE001 — Qt antigo ou sem aplicação
        return False


def set_dark(dark: bool) -> None:
    global _dark
    _dark = bool(dark)


def is_dark() -> bool:
    return _dark


def c(color: str) -> str:
    """Devolve a cor do tema ativo para uma cor escrita no tema claro."""
    if not _dark:
        return color
    return DARK.get(color.lower(), color)


def qcolor(color: str):
    from PySide6.QtGui import QColor

    return QColor(c(color))


def recolor(text: str) -> str:
    """Troca todas as cores #rrggbb de um texto (QSS, SVG) pelas do tema ativo."""
    if not _dark:
        return text
    return _HEX.sub(lambda m: DARK.get(m.group(0).lower(), m.group(0)), text)


def themed_asset_dir(ui_dir: str, names=("check.svg", "arrow-up.svg", "arrow-down.svg")) -> str:
    """Pasta com os SVG usados pelo QSS já recoloridos para o tema ativo.

    No tema claro é a própria pasta ui/. No escuro, os ficheiros são copiados
    (recoloridos) para uma pasta temporária.
    """
    if not _dark:
        return ui_dir
    out = os.path.join(tempfile.gettempdir(), "SafeOti-tema-escuro")
    try:
        os.makedirs(out, exist_ok=True)
        for name in names:
            src = os.path.join(ui_dir, name)
            with open(src, encoding="utf-8") as fh:
                svg = fh.read()
            svg = _recolor_svg(svg)
            with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
                fh.write(svg)
    except OSError as exc:
        log.warning("Não foi possível preparar os ícones do tema escuro: %s", exc)
        return ui_dir
    return out


def _recolor_svg(svg: str) -> str:
    # Os SVG usam nomes/hex curtos às vezes; normaliza os mais comuns.
    for short, full in (('"black"', '"#000000"'), ('"white"', '"#ffffff"'), ('"#000"', '"#000000"'), ('"#fff"', '"#ffffff"')):
        svg = svg.replace(short, full)
    return recolor(svg)


def apply_palette(app) -> None:
    """Paleta Qt coerente com o tema (menus de contexto, diálogos nativos, etc.)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette

    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):
        try:
            hints.setColorScheme(Qt.ColorScheme.Dark if _dark else Qt.ColorScheme.Light)
        except Exception:  # noqa: BLE001
            pass
    text = QColor(c("#111111"))
    base = QColor(c("#ffffff"))
    palette = QPalette()
    for role, color in (
        (QPalette.Window, base),
        (QPalette.WindowText, text),
        (QPalette.Base, base),
        (QPalette.AlternateBase, QColor(c("#fafafa"))),
        (QPalette.Text, text),
        (QPalette.Button, base),
        (QPalette.ButtonText, text),
        (QPalette.ToolTipBase, text),
        (QPalette.ToolTipText, base),
        (QPalette.Highlight, QColor(c("#000000"))),
        (QPalette.HighlightedText, base),
        (QPalette.PlaceholderText, QColor(c("#9a9a9a"))),
        (QPalette.Link, text),
    ):
        palette.setColor(role, color)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(c("#b5b5b5")))
    app.setPalette(palette)


def load_stylesheet(ui_dir: str) -> str:
    """Lê ui/styles.qss, substitui {UI_DIR} e aplica as cores do tema ativo."""
    path = os.path.join(ui_dir, "styles.qss")
    try:
        with open(path, encoding="utf-8") as fh:
            qss = fh.read()
    except OSError as exc:
        log.warning("Folha de estilo não encontrada (%s): %s", path, exc)
        return ""
    asset_dir = themed_asset_dir(ui_dir).replace("\\", "/")
    return recolor(qss.replace("{UI_DIR}", asset_dir))


def apply(app, choice: str) -> None:
    """Aplica o tema escolhido à aplicação inteira (paleta + QSS)."""
    from ui.common import resource_path

    set_dark(resolve(choice))
    apply_palette(app)
    app.setStyleSheet(load_stylesheet(resource_path("ui")))
    log.info("Tema aplicado: %s (%s)", choice, "escuro" if _dark else "claro")
