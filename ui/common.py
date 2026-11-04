"""Widgets e funções auxiliares partilhados pelas páginas da interface."""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import admin
from core.i18n import format_decimal, tr

log = logging.getLogger("safeoti.ui")


def resource_path(relative: str) -> str:
    """Caminho de um recurso, quer a correr do código-fonte quer do .exe do PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, relative)


def fmt_number(value: float, decimals: int = 1) -> str:
    """Número com o separador decimal do idioma ativo (vírgula em pt-PT)."""
    return format_decimal(value, decimals)


def make_app_icon() -> QIcon:
    """Ícone da aplicação: escudo branco num quadrado preto (desenhado em código)."""
    from ui.icons import app_icon

    return app_icon()


# --------------------------------------------------------------------------- #
# Caixas de diálogo (texto em português, botões "Sim"/"Não")
# --------------------------------------------------------------------------- #
def confirm(parent: Optional[QWidget], title: str, text: str, detail: str = "",
            yes_text: str = "", no_text: str = "", danger: bool = True) -> bool:
    yes_text = yes_text or tr("Sim")
    no_text = no_text or tr("Não")
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    yes = box.addButton(yes_text, QMessageBox.YesRole)
    no = box.addButton(no_text, QMessageBox.NoRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes


def show_info(parent: Optional[QWidget], title: str, text: str, detail: str = "") -> None:
    _message(parent, QMessageBox.Information, title, text, detail)


def show_warning(parent: Optional[QWidget], title: str, text: str, detail: str = "") -> None:
    _message(parent, QMessageBox.Warning, title, text, detail)


def show_error(parent: Optional[QWidget], title: str, text: str, detail: str = "") -> None:
    _message(parent, QMessageBox.Critical, title, text, detail)


def _message(parent, icon, title, text, detail) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(icon)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    box.addButton(tr("OK"), QMessageBox.AcceptRole)
    box.exec()


def request_admin_relaunch(parent: Optional[QWidget]) -> None:
    """Pede confirmação e reinicia o SafeOti com privilégios de administrador."""
    if not confirm(
        parent,
        tr("Reiniciar como administrador"),
        tr("O SafeOti vai fechar e abrir novamente com privilégios de administrador."),
        tr("O Windows vai pedir confirmação (Controlo de Conta de Utilizador). Continuar?"),
        yes_text=tr("Reiniciar"),
        no_text=tr("Cancelar"),
        danger=False,
    ):
        return
    log.info("Utilizador pediu reinício como administrador.")
    if admin.relaunch_as_admin():
        window = parent.window() if parent is not None else None
        if window is not None:
            setattr(window, "_skip_close_prompts", True)
            window.close()
        else:
            QApplication.quit()
    else:
        show_warning(
            parent,
            tr("Reiniciar como administrador"),
            tr("Não foi possível reiniciar como administrador."),
            tr("O pedido pode ter sido recusado na janela do Controlo de Conta de Utilizador."),
        )


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #
class AdminNotice(QFrame):
    """Aviso discreto nas páginas com funções que exigem administrador (só se não for admin)."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from ui.icons import pixmap

        self.setObjectName("AdminNotice")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(10)
        icon_label = QLabel()
        icon_label.setPixmap(pixmap("cadeado", "#111111", 16))
        icon_label.setFixedSize(18, 18)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setObjectName("AdminNoticeText")
        action = QPushButton(tr("Reiniciar como administrador  →"))
        action.setProperty("variant", "link")
        action.setCursor(Qt.PointingHandCursor)
        action.clicked.connect(lambda: request_admin_relaunch(self))
        layout.addWidget(icon_label, 0, Qt.AlignVCenter)
        layout.addWidget(label, 1)
        layout.addWidget(action, 0, Qt.AlignVCenter)
        self.setVisible(not admin.is_admin())


class StatCard(QFrame):
    """Cartão com título, valor principal e linha secundária."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        self.value = QLabel("—")
        self.value.setObjectName("CardValue")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("CardSubtitle")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.subtitle)
        layout.addStretch(1)

    def set_values(self, value: str, subtitle: str = "") -> None:
        self.value.setText(value)
        self.subtitle.setText(subtitle)


def page_header(title: str, subtitle: str) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 8)
    layout.setSpacing(4)
    title_label = QLabel(title)
    title_label.setObjectName("PageTitle")
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("PageSubtitle")
    subtitle_label.setWordWrap(True)
    layout.addWidget(title_label)
    layout.addWidget(subtitle_label)
    return widget


def section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def button(text: str, variant: str = "", tooltip: str = "", icon_name: str = "") -> QPushButton:
    btn = QPushButton(text)
    if icon_name:
        from ui.icons import icon

        color = "#ffffff" if variant == "primary" else "#111111"
        btn.setIcon(icon(icon_name, color, 16))
    if variant:
        btn.setProperty("variant", variant)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class StatusLine(QLabel):
    """Linha de estado de uma página (cores por tipo de mensagem)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusLine")
        self.setWordWrap(True)

    def set_message(self, text: str, kind: str = "info") -> None:
        self.setProperty("kind", kind)
        self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)


def bold_font(widget: QWidget, point_delta: int = 0) -> QFont:
    font = QFont(widget.font())
    font.setBold(True)
    if point_delta:
        font.setPointSize(font.pointSize() + point_delta)
    return font


PAGE_MARGINS = (32, 28, 32, 24)
PAGE_SPACING = 16


def page_layout(widget: QWidget) -> QVBoxLayout:
    """Layout vertical com as margens e o espaçamento comuns a todas as páginas."""
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(*PAGE_MARGINS)
    layout.setSpacing(PAGE_SPACING)
    return layout


def style_table(view) -> None:
    """Aspeto limpo e comum para tabelas e listas em árvore."""
    from PySide6.QtWidgets import QAbstractItemView, QTableView

    view.setAlternatingRowColors(False)
    view.setFrameShape(QFrame.NoFrame)
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    if isinstance(view, QTableView):
        view.setShowGrid(False)
        view.verticalHeader().setDefaultSectionSize(38)
        view.verticalHeader().hide()
        header = view.horizontalHeader()
    else:
        header = view.header()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    header.setMinimumHeight(36)


class PageBase(QWidget):
    """Base das páginas: sinal para mensagens na barra de estado."""

    status_message = Signal(str)
