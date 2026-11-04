"""Janela do instalador: sem moldura do Windows, cantos arredondados, sombra,
barra lateral preta com os passos e páginas com transição suave.

Segue o tema da app (branco com detalhes pretos) e reutiliza o logótipo e os
ícones de ui/icons.py.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import (
    Property, QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, QSize, Qt, Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from ui.icons import logo_pixmap, pixmap

INK = "#111111"
MUTED = "#8a8a8a"
LINE = "#ececec"
SHADOW = 18  # margem transparente à volta do cartão, para a sombra

QSS = """
* { font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif; font-size: 10pt; color: #111111; }
QFrame#Card { background: #ffffff; border: 1px solid #e4e4e4; border-radius: 16px; }
QFrame#Side { background: #111111; border-top-left-radius: 15px; border-bottom-left-radius: 15px; }
QLabel#Brand { color: #ffffff; font-size: 17pt; font-weight: 700; }
QLabel#BrandSub { color: #8f8f8f; font-size: 8.5pt; }
QLabel#SideFoot { color: #6f6f6f; font-size: 8pt; }
QLabel#Title { font-size: 19pt; font-weight: 700; color: #000000; }
QLabel#Lead { color: #6b6b6b; font-size: 10.5pt; }
QLabel#Muted { color: #8a8a8a; }
QLabel#Small { color: #8a8a8a; font-size: 8.5pt; }
QLabel#FeatTitle { font-weight: 600; }
QLabel#Big { font-size: 34pt; font-weight: 700; color: #000000; }
QLabel#Error { color: #b42318; background: #fef3f2; border: 1px solid #fecdca; border-radius: 10px; padding: 10px 12px; }
QFrame#Field { background: #fafafa; border: 1px solid #e6e6e6; border-radius: 10px; }
QFrame#Option { background: #ffffff; border: 1px solid #ececec; border-radius: 12px; }
QPushButton { border-radius: 10px; padding: 0 18px; min-height: 38px; font-weight: 600; }
QPushButton#Primary { background: #111111; color: #ffffff; border: none; }
QPushButton#Primary:hover { background: #2a2a2a; }
QPushButton#Primary:pressed { background: #000000; }
QPushButton#Primary:disabled { background: #cfcfcf; color: #f5f5f5; }
QPushButton#Secondary { background: #ffffff; color: #111111; border: 1px solid #dedede; }
QPushButton#Secondary:hover { background: #f5f5f5; }
QPushButton#Secondary:disabled { color: #bdbdbd; border-color: #eeeeee; }
QPushButton#Link { background: transparent; border: none; color: #111111; padding: 0 6px; min-height: 28px; text-decoration: underline; }
QPushButton#WinBtn { background: transparent; border: none; border-radius: 8px; min-height: 30px; min-width: 34px; padding: 0; }
QPushButton#WinBtn:hover { background: #f0f0f0; }
QPushButton#WinClose:hover { background: #e5484d; }
"""


# ---------------------------------------------------------------- widgets

class Toggle(QAbstractButton):
    """Interruptor animado (em vez da caixa de verificação do Windows)."""

    def __init__(self, checked: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _get(self) -> float:
        return self._pos

    def _set(self, value: float) -> None:
        self._pos = value
        self.update()

    knob = Property(float, _get, _set)

    def sizeHint(self) -> QSize:
        return QSize(42, 24)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off, on = QColor("#d9d9d9"), QColor(INK)
        track = QColor(
            int(off.red() + (on.red() - off.red()) * self._pos),
            int(off.green() + (on.green() - off.green()) * self._pos),
            int(off.blue() + (on.blue() - off.blue()) * self._pos),
        )
        if not self.isEnabled():
            track.setAlpha(110)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 42, 24), 12, 12)
        p.setBrush(QColor("#ffffff"))
        x = 3 + self._pos * 18
        p.drawEllipse(QRectF(x, 3, 18, 18))


class ProgressLine(QWidget):
    """Barra de progresso fina, com avanço suave e brilho a deslizar."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(8)
        self._value = 0.0
        self._shine = 0.0
        self._anim = QPropertyAnimation(self, b"value", self)
        self._anim.setDuration(350)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._shine_anim = QPropertyAnimation(self, b"shine", self)
        self._shine_anim.setStartValue(-0.3)
        self._shine_anim.setEndValue(1.3)
        self._shine_anim.setDuration(1400)
        self._shine_anim.setLoopCount(-1)

    def _get(self) -> float:
        return self._value

    def _set(self, v: float) -> None:
        self._value = max(0.0, min(1.0, v))
        self.update()

    value = Property(float, _get, _set)

    def _gs(self) -> float:
        return self._shine

    def _ss(self, v: float) -> None:
        self._shine = v
        self.update()

    shine = Property(float, _gs, _ss)

    def set_target(self, v: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._value)
        self._anim.setEndValue(v)
        self._anim.start()

    def set_running(self, running: bool) -> None:
        if running:
            self._shine_anim.start()
        else:
            self._shine_anim.stop()
            self._shine = -1
            self.update()

    def reset(self) -> None:
        self._anim.stop()
        self._set(0.0)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#efefef"))
        p.drawRoundedRect(r, rad, rad)
        if self._value <= 0:
            return
        fill = QRectF(0, 0, max(r.height(), r.width() * self._value), r.height())
        path = QPainterPath()
        path.addRoundedRect(fill, rad, rad)
        p.fillPath(path, QColor(INK))
        if -0.3 < self._shine < 1.3:
            from PySide6.QtGui import QLinearGradient

            cx = fill.width() * self._shine
            g = QLinearGradient(cx - 60, 0, cx + 60, 0)
            g.setColorAt(0, QColor(255, 255, 255, 0))
            g.setColorAt(0.5, QColor(255, 255, 255, 70))
            g.setColorAt(1, QColor(255, 255, 255, 0))
            p.setClipPath(path)
            p.fillRect(fill, g)


class SuccessMark(QWidget):
    """Círculo que se desenha e um visto que aparece — animação de conclusão."""

    def __init__(self, size: int = 76, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._t = 0.0
        self._anim = QPropertyAnimation(self, b"t", self)
        self._anim.setDuration(900)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _get(self) -> float:
        return self._t

    def _set(self, v: float) -> None:
        self._t = v
        self.update()

    t = Property(float, _get, _set)

    def play(self) -> None:
        self._anim.stop()
        self._anim.start()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.width()
        ring = min(1.0, self._t / 0.55)
        fill = max(0.0, min(1.0, (self._t - 0.45) / 0.25))
        check = max(0.0, (self._t - 0.6) / 0.4)
        rect = QRectF(3, 3, s - 6, s - 6)
        if fill > 0:
            p.setPen(Qt.NoPen)
            c = QColor(INK)
            c.setAlphaF(fill)
            p.setBrush(c)
            p.drawEllipse(rect)
        pen = QPen(QColor(INK), 3, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawArc(rect, 90 * 16, int(-360 * 16 * ring))
        if check > 0:
            pts = [QPointF(s * 0.30, s * 0.52), QPointF(s * 0.44, s * 0.65), QPointF(s * 0.71, s * 0.37)]
            path = QPainterPath(pts[0])
            first = min(1.0, check / 0.4)
            path.lineTo(pts[0] + (pts[1] - pts[0]) * first)
            if check > 0.4:
                second = (check - 0.4) / 0.6
                path.lineTo(pts[1] + (pts[2] - pts[1]) * second)
            p.setPen(QPen(QColor("#ffffff"), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)


class StepDot(QWidget):
    """Indicador de um passo na barra lateral: futuro, atual ou concluído."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.state = "todo"

    def set_state(self, state: str) -> None:
        self.state = state
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(2, 2, 18, 18)
        if self.state == "done":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(r)
            p.setPen(QPen(QColor(INK), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            path = QPainterPath(QPointF(7, 11.2))
            path.lineTo(QPointF(9.8, 14))
            path.lineTo(QPointF(15.2, 8.4))
            p.drawPath(path)
        elif self.state == "current":
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r.adjusted(1, 1, -1, -1))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(QRectF(8, 8, 6, 6))
        else:
            p.setPen(QPen(QColor("#4a4a4a"), 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r.adjusted(1, 1, -1, -1))


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def icon_label(name: str, size: int = 20, color: str = INK) -> QLabel:
    lbl = QLabel()
    lbl.setPixmap(pixmap(name, color, size))
    lbl.setFixedSize(size + 4, size + 4)
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


def inverted_logo(size: int) -> QLabel:
    """Logótipo em branco (quadrado branco, escudo preto) para a barra lateral escura."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QPixmap
    from PySide6.QtSvg import QSvgRenderer

    from ui.icons import _svg

    scale = 2
    full = size * scale
    pm = QPixmap(full, full)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, full, full), full * 0.26, full * 0.26)
    p.fillPath(path, QColor("#ffffff"))
    margin = full * 0.2
    QSvgRenderer(QByteArray(_svg("escudo", INK, 2.0))).render(
        p, QRectF(margin, margin, full - 2 * margin, full - 2 * margin)
    )
    p.end()
    pm.setDevicePixelRatio(scale)
    lbl = QLabel()
    lbl.setPixmap(pm)
    lbl.setFixedSize(size, size)
    return lbl


def option_row(icon: str, title: str, subtitle: str, toggle: Toggle) -> QFrame:
    row = QFrame()
    row.setObjectName("Option")
    lay = QHBoxLayout(row)
    lay.setContentsMargins(14, 10, 14, 10)
    lay.setSpacing(12)
    lay.addWidget(icon_label(icon, 18))
    text = QVBoxLayout()
    text.setSpacing(0)
    text.addWidget(label(title, "FeatTitle"))
    text.addWidget(label(subtitle, "Small"))
    lay.addLayout(text, 1)
    lay.addWidget(toggle)
    row.mousePressEvent = lambda _e: toggle.toggle() if toggle.isEnabled() else None  # type: ignore[method-assign]
    row.setCursor(Qt.PointingHandCursor)
    return row


# ---------------------------------------------------------------- janela

class _WinButton(QPushButton):
    def __init__(self, kind: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setObjectName("WinBtn")
        if kind == "close":
            self.setProperty("class", "close")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(34, 30)
        self._hover = False

    def enterEvent(self, e) -> None:
        self._hover = True
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        super().leaveEvent(e)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._hover and self.isEnabled():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e5484d" if self.kind == "close" else "#f0f0f0"))
            p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        color = "#ffffff" if (self._hover and self.kind == "close") else ("#111111" if self.isEnabled() else "#c8c8c8")
        p.setPen(QPen(QColor(color), 1.5, Qt.SolidLine, Qt.RoundCap))
        cx, cy = self.width() / 2, self.height() / 2
        if self.kind == "close":
            p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy + 5))
            p.drawLine(QPointF(cx + 5, cy - 5), QPointF(cx - 5, cy + 5))
        else:
            p.drawLine(QPointF(cx - 5, cy), QPointF(cx + 5, cy))


class Shell(QWidget):
    """Janela base: barra lateral com passos + área de páginas + rodapé de botões."""

    closeRequested = Signal()

    def __init__(self, subtitle: str, steps: List[str]) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(QSS)
        self.resize(780 + 2 * SHADOW, 500 + 2 * SHADOW)
        self._drag: Optional[QPoint] = None
        self.can_close = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)
        card = QFrame()
        card.setObjectName("Card")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(SHADOW * 2)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 60))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        root = QHBoxLayout(card)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- barra lateral
        side = QFrame()
        side.setObjectName("Side")
        side.setFixedWidth(240)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(28, 30, 24, 24)
        sl.setSpacing(0)
        sl.addWidget(inverted_logo(46))
        sl.addSpacing(16)
        sl.addWidget(label("SafeOti", "Brand"))
        sl.addWidget(label(subtitle, "BrandSub"))
        sl.addSpacing(36)
        self._dots: List[StepDot] = []
        self._step_labels: List[QLabel] = []
        for name in steps:
            row = QHBoxLayout()
            row.setSpacing(12)
            dot = StepDot()
            lbl = QLabel(name)
            row.addWidget(dot)
            row.addWidget(lbl, 1)
            sl.addLayout(row)
            sl.addSpacing(14)
            self._dots.append(dot)
            self._step_labels.append(lbl)
        sl.addStretch(1)
        sl.addWidget(label("Limpar · Monitorizar · Otimizar", "SideFoot"))
        root.addWidget(side)

        # --- conteúdo
        main = QVBoxLayout()
        main.setContentsMargins(36, 10, 12, 26)
        main.setSpacing(0)
        bar = QHBoxLayout()
        bar.addStretch(1)
        self.btn_min = _WinButton("min")
        self.btn_close = _WinButton("close")
        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_close.clicked.connect(self._try_close)
        bar.addWidget(self.btn_min)
        bar.addWidget(self.btn_close)
        main.addLayout(bar)

        body = QVBoxLayout()
        body.setContentsMargins(0, 6, 24, 0)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        self.footer = QHBoxLayout()
        self.footer.setSpacing(10)
        self.btn_back = QPushButton("Voltar")
        self.btn_back.setObjectName("Secondary")
        self.btn_next = QPushButton("Seguinte")
        self.btn_next.setObjectName("Primary")
        for b in (self.btn_back, self.btn_next):
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumWidth(116)
        self.footer.addStretch(1)
        self.footer.addWidget(self.btn_back)
        self.footer.addWidget(self.btn_next)
        body.addSpacing(18)
        body.addLayout(self.footer)
        main.addLayout(body, 1)
        root.addLayout(main, 1)

        self._fade: Optional[QPropertyAnimation] = None

    # --- passos e páginas
    def set_step(self, index: int) -> None:
        for i, (dot, lbl) in enumerate(zip(self._dots, self._step_labels)):
            state = "done" if i < index else ("current" if i == index else "todo")
            dot.set_state(state)
            color = "#ffffff" if i == index else ("#c9c9c9" if i < index else "#6f6f6f")
            weight = "600" if i == index else "400"
            lbl.setStyleSheet(f"color: {color}; font-weight: {weight};")

    def add_page(self, page: QWidget) -> int:
        page.setAutoFillBackground(False)
        return self.stack.addWidget(page)

    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        page = self.stack.currentWidget()
        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", page)
        fade.setDuration(260)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        fade.finished.connect(lambda: page.setGraphicsEffect(None))
        slide = QPropertyAnimation(page, b"pos", page)
        slide.setDuration(260)
        end = page.pos()
        slide.setStartValue(end + QPoint(18, 0))
        slide.setEndValue(end)
        slide.setEasingCurve(QEasingCurve.OutCubic)
        fade.start()
        slide.start()
        self._fade = fade

    # --- janela sem moldura: arrastar e fechar
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and e.position().y() < SHADOW + 60:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    def _try_close(self) -> None:
        if self.can_close:
            self.closeRequested.emit()
            self.close()

    def closeEvent(self, e) -> None:
        if not self.can_close:
            e.ignore()
            return
        super().closeEvent(e)

    def appear(self) -> None:
        self.setWindowOpacity(0.0)
        self.show()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(220)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start()
        self._appear = anim


def prepare_app(app) -> None:
    """Fusion + paleta clara, mesmo com o Windows em modo escuro."""
    from PySide6.QtGui import QPalette

    app.setStyle("Fusion")
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except Exception:  # noqa: BLE001 — Qt < 6.8
        pass
    pal = QPalette()
    white, black = QColor("#ffffff"), QColor(INK)
    for role, color in (
        (QPalette.Window, white), (QPalette.WindowText, black), (QPalette.Base, white),
        (QPalette.AlternateBase, QColor("#fafafa")), (QPalette.Text, black), (QPalette.Button, white),
        (QPalette.ButtonText, black), (QPalette.Highlight, black), (QPalette.HighlightedText, white),
        (QPalette.ToolTipBase, black), (QPalette.ToolTipText, white),
    ):
        pal.setColor(role, color)
    app.setPalette(pal)


def page_layout(page: QWidget) -> QVBoxLayout:
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    return lay


def fixed_font(size: float, weight: int = 400) -> QFont:
    f = QFont("Segoe UI Variable Display")
    f.setPointSizeF(size)
    f.setWeight(QFont.Weight(weight))
    return f


def spacer(h: int) -> QWidget:
    w = QWidget()
    w.setFixedHeight(h)
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return w
