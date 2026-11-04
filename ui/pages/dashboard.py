"""Página "Painel": monitorização do sistema em tempo real."""

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Dict, List, Optional

import pyqtgraph as pg
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.i18n import tr
from core.monitor import DiskUsage, Snapshot
from core.utils import format_bytes, format_rate
from ui import theme
from ui.common import PAGE_MARGINS, PAGE_SPACING, PageBase, StatCard, fmt_number, page_header, section_label

HISTORY_SECONDS = 60

# Tema monocromático: primeira série a preto contínuo, segunda a cinzento tracejado.
COLOR_MAIN = "#111111"
COLOR_SECOND = "#8a8a8a"

pg.setConfigOptions(antialias=True)


# --------------------------------------------------------------------------- #
# Widgets de apoio
# --------------------------------------------------------------------------- #
class MiniBar(QWidget):
    """Barra fina de percentagem, desenhada à mão (mais leve e limpa que QProgressBar)."""

    def __init__(self, height: int = 6, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._alert = False
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, value: float, alert: bool = False) -> None:
        value = max(0.0, min(100.0, float(value)))
        if abs(value - self._value) > 0.05 or alert != self._alert:
            self._value = value
            self._alert = alert
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, self.height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QColor(theme.c("#eeeeee")))
        if self._value > 0:
            width = max(rect.height(), rect.width() * self._value / 100)
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(rect.x(), rect.y(), width, rect.height()), radius, radius)
            painter.fillPath(fill, QColor(theme.c("#000000" if self._alert else "#111111")))
        painter.end()


class CoreCell(QWidget):
    def __init__(self, index: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(4)
        name = QLabel(tr("Núcleo {n}", n=index + 1))
        name.setObjectName("MiniCaption")
        self.value = QLabel("0 %")
        self.value.setObjectName("MiniValue")
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(self.value)
        layout.addLayout(top)
        self.bar = MiniBar(5)
        layout.addWidget(self.bar)

    def set_value(self, value: float) -> None:
        self.value.setText(f"{int(round(value))} %")
        self.bar.set_value(value)


class DiskRow(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.name = QLabel()
        self.name.setObjectName("DiskName")
        self.detail = QLabel()
        self.detail.setObjectName("MiniCaption")
        self.percent = QLabel()
        self.percent.setObjectName("MiniValue")
        top.addWidget(self.name)
        top.addSpacing(8)
        top.addWidget(self.detail, 1)
        top.addWidget(self.percent)
        layout.addLayout(top)
        self.bar = MiniBar(6)
        layout.addWidget(self.bar)

    def set_disk(self, disk: DiskUsage) -> None:
        self.name.setText(disk.mountpoint.rstrip("\\") or disk.mountpoint)
        fs = f"{disk.fstype}  ·  " if disk.fstype else ""
        self.detail.setText(fs + tr("{free} livres de {total}", free=format_bytes(disk.free), total=format_bytes(disk.total)))
        self.percent.setText(f"{fmt_number(disk.percent, 0)} %")
        self.bar.set_value(disk.percent, alert=disk.percent >= 90)


class ChartCard(QFrame):
    """Cartão com título, legenda/valores à direita e um gráfico pyqtgraph sem moldura."""

    def __init__(self, title: str, unit: str, fixed_max: Optional[float] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 14, 10)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title_label = QLabel(f"{title}  <span style='color:#a3a3a3;font-weight:400'>{unit}</span>")
        title_label.setObjectName("ChartTitle")
        title_label.setTextFormat(Qt.RichText)
        self.legend = QLabel()
        self.legend.setObjectName("ChartLegend")
        self.legend.setTextFormat(Qt.RichText)
        header.addWidget(title_label)
        header.addStretch(1)
        header.addWidget(self.legend)
        layout.addLayout(header)

        self.plot = pg.PlotWidget(background=theme.c("#ffffff"))
        self.plot.setFixedHeight(170)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setFrameShape(QFrame.NoFrame)
        item = self.plot.getPlotItem()
        item.setContentsMargins(0, 0, 0, 0)
        item.showGrid(x=False, y=True, alpha=0.07)
        tick_font = QFont()
        tick_font.setPointSize(8)
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen(theme.c("#ececec")))
            axis.setTextPen(pg.mkPen(theme.c("#9a9a9a")))
            axis.setStyle(tickFont=tick_font, tickLength=0, tickTextOffset=6)
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.plot.getAxis("left").setWidth(42)
        self.plot.getAxis("bottom").setTicks([[(-60, "60 s"), (-45, "45 s"), (-30, "30 s"), (-15, "15 s"), (0, tr("agora"))]])
        self.plot.setXRange(-HISTORY_SECONDS, 0, padding=0)
        if fixed_max is not None:
            self.plot.setYRange(0, fixed_max, padding=0.02)
            self.plot.getAxis("left").setTicks([[(0, "0"), (50, "50"), (100, "100")]])
        else:
            self.plot.enableAutoRange(axis="y", enable=True)
            self.plot.setLimits(yMin=0)
        item.getViewBox().setDefaultPadding(0.04)
        layout.addWidget(self.plot)

    def add_curve(self, color: str, fill: bool = False, dashed: bool = False) -> pg.PlotDataItem:
        pen = pg.mkPen(color=color, width=1.8, style=Qt.DashLine if dashed else Qt.SolidLine)
        kwargs = {"pen": pen}
        if fill:
            kwargs.update(fillLevel=0, brush=pg.mkBrush("#00000010"))
        return self.plot.plot([], [], **kwargs)


def _legend(items) -> str:
    """Legenda em HTML: [(rótulo, valor, tracejado)]."""
    parts = []
    for label, value, dashed in items:
        mark = "┅" if dashed else "━"
        color = theme.c(COLOR_SECOND if dashed else COLOR_MAIN)
        parts.append(
            f"<span style='color:{color}'>{mark}</span>&nbsp;<span style='color:#8a8a8a'>{label}</span>"
            f"&nbsp;<b style='color:#111111'>{value}</b>"
        )
    return "&nbsp;&nbsp;&nbsp;&nbsp;".join(parts)


# --------------------------------------------------------------------------- #
# Página
# --------------------------------------------------------------------------- #
class DashboardPage(PageBase):
    def __init__(self, interval_ms: int = 1000, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._interval_ms = interval_ms
        self._times: Deque[float] = deque()
        self._series: Dict[str, Deque[float]] = {}
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._core_cells: List[CoreCell] = []
        self._disk_rows: List[DiskRow] = []
        self._io_labels: List[QLabel] = []
        self._columns = None
        self._core_columns = 0
        self._last: Optional[Snapshot] = None
        self._build()
        self._reset_history()

    # ------------------------------------------------------------------ UI --
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        content.setObjectName("PageContent")
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(tr("Painel"), tr("Estado do sistema em tempo real.")))

        # Cartões de resumo
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(14)
        self.card_cpu = StatCard(tr("Processador"))
        self.card_ram = StatCard(tr("Memória"))
        self.card_disk = StatCard(tr("Leitura do disco"))
        self.card_net = StatCard(tr("Download"))
        self.cpu_bar = MiniBar(4)
        self.ram_bar = MiniBar(4)
        self.card_cpu.layout().addSpacing(6)
        self.card_cpu.layout().addWidget(self.cpu_bar)
        self.card_ram.layout().addSpacing(6)
        self.card_ram.layout().addWidget(self.ram_bar)
        self._cards = [self.card_cpu, self.card_ram, self.card_disk, self.card_net]
        layout.addLayout(self.cards_grid)

        # Gráficos
        self.graphs_grid = QGridLayout()
        self.graphs_grid.setSpacing(14)
        self.chart_cpu = ChartCard(tr("Processador"), "%", 100)
        self.chart_ram = ChartCard(tr("Memória"), "%", 100)
        self.chart_net = ChartCard(tr("Rede"), "KB/s")
        self.chart_disk = ChartCard(tr("Disco"), "MB/s")
        self._curves["cpu"] = self.chart_cpu.add_curve(COLOR_MAIN, fill=True)
        self._curves["ram"] = self.chart_ram.add_curve(COLOR_MAIN, fill=True)
        self._curves["down"] = self.chart_net.add_curve(COLOR_MAIN)
        self._curves["up"] = self.chart_net.add_curve(COLOR_SECOND, dashed=True)
        self._curves["read"] = self.chart_disk.add_curve(COLOR_MAIN)
        self._curves["write"] = self.chart_disk.add_curve(COLOR_SECOND, dashed=True)
        self._charts = [self.chart_cpu, self.chart_ram, self.chart_net, self.chart_disk]
        layout.addLayout(self.graphs_grid)

        # Unidades + discos físicos lado a lado (empilham em janelas estreitas)
        self.storage_grid = QGridLayout()
        self.storage_grid.setSpacing(14)

        self.disks_card = QFrame()
        self.disks_card.setObjectName("Card")
        disks_layout = QVBoxLayout(self.disks_card)
        disks_layout.setContentsMargins(18, 16, 18, 18)
        disks_layout.setSpacing(16)
        disks_title = QLabel(tr("Unidades"))
        disks_title.setObjectName("ChartTitle")
        disks_layout.addWidget(disks_title)
        self.disks_box = QVBoxLayout()
        self.disks_box.setSpacing(16)
        disks_layout.addLayout(self.disks_box)
        disks_layout.addStretch(1)

        self.io_card = QFrame()
        self.io_card.setObjectName("Card")
        io_layout = QVBoxLayout(self.io_card)
        io_layout.setContentsMargins(18, 16, 18, 18)
        io_layout.setSpacing(10)
        io_title = QLabel(tr("Velocidade por disco físico"))
        io_title.setObjectName("ChartTitle")
        io_layout.addWidget(io_title)
        self.io_box = QVBoxLayout()
        self.io_box.setSpacing(8)
        io_layout.addLayout(self.io_box)
        io_layout.addStretch(1)
        layout.addLayout(self.storage_grid)

        # Núcleos
        self.cores_card = QFrame()
        self.cores_card.setObjectName("Card")
        cores_outer = QVBoxLayout(self.cores_card)
        cores_outer.setContentsMargins(18, 16, 18, 18)
        cores_outer.setSpacing(14)
        cores_title = QLabel(tr("Utilização por núcleo"))
        cores_title.setObjectName("ChartTitle")
        cores_outer.addWidget(cores_title)
        self.cores_layout = QGridLayout()
        self.cores_layout.setHorizontalSpacing(24)
        self.cores_layout.setVerticalSpacing(14)
        cores_outer.addLayout(self.cores_layout)
        layout.addWidget(self.cores_card)

        # Temperaturas (escondidas se não houver dados)
        self.temps_title = section_label(tr("Temperaturas"))
        self.temps_label = QLabel()
        self.temps_label.setObjectName("Card")
        self.temps_label.setWordWrap(True)
        self.temps_label.setContentsMargins(18, 14, 18, 14)
        self.temps_title.hide()
        self.temps_label.hide()
        layout.addWidget(self.temps_title)
        layout.addWidget(self.temps_label)
        layout.addStretch(1)

        self._relayout(1200)

    def _relayout(self, width: int) -> None:
        """Reorganiza cartões e gráficos conforme a largura disponível."""
        card_cols = 4 if width >= 1000 else 2 if width >= 560 else 1
        plot_cols = 2 if width >= 800 else 1
        if (card_cols, plot_cols) == self._columns:
            return
        self._columns = (card_cols, plot_cols)
        for grid, widgets, cols in (
            (self.cards_grid, self._cards, card_cols),
            (self.graphs_grid, self._charts, plot_cols),
            (self.storage_grid, [self.disks_card, self.io_card], plot_cols),
        ):
            for widget in widgets:
                grid.removeWidget(widget)
            for index, widget in enumerate(widgets):
                grid.addWidget(widget, index // cols, index % cols)
            for col in range(4):
                grid.setColumnStretch(col, 1 if col < cols else 0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().resizeEvent(event)
        self._relayout(event.size().width())

    # ------------------------------------------------------------- dados --
    def set_interval(self, interval_ms: int) -> None:
        self._interval_ms = interval_ms
        self._reset_history()

    def _reset_history(self) -> None:
        points = max(2, math.ceil(HISTORY_SECONDS * 1000 / self._interval_ms) + 1)
        self._times = deque(maxlen=points)
        self._series = {k: deque(maxlen=points) for k in ("cpu", "ram", "down", "up", "read", "write")}

    def _ensure_core_cells(self, count: int) -> None:
        if len(self._core_cells) == count:
            return
        for cell in self._core_cells:
            cell.setParent(None)
            cell.deleteLater()
        self._core_cells = []
        columns = 4 if count <= 8 else 6 if count <= 24 else 8
        for index in range(count):
            cell = CoreCell(index)
            self.cores_layout.addWidget(cell, index // columns, index % columns)
            self._core_cells.append(cell)

    def _ensure_rows(self, rows: list, box: QVBoxLayout, count: int, factory) -> None:
        while len(rows) < count:
            widget = factory()
            box.addWidget(widget)
            rows.append(widget)
        while len(rows) > count:
            widget = rows.pop()
            widget.setParent(None)
            widget.deleteLater()

    def update_snapshot(self, snap: Snapshot) -> None:
        self._append(snap)
        if self.isVisible():  # com a página escondida guarda-se só o histórico
            self._redraw(snap)

    def _append(self, snap: Snapshot) -> None:
        self._times.append(snap.timestamp)
        self._series["cpu"].append(snap.cpu_total)
        self._series["ram"].append(snap.ram_percent)
        self._series["down"].append(snap.net_down_bps / 1024)
        self._series["up"].append(snap.net_up_bps / 1024)
        self._series["read"].append(snap.disk_read_bps / (1024 * 1024))
        self._series["write"].append(snap.disk_write_bps / (1024 * 1024))
        self._last = snap

    def showEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().showEvent(event)
        if self._last is not None:
            self._redraw(self._last)

    def _redraw(self, snap: Snapshot) -> None:
        if self._times:
            now = self._times[-1]
            xs = [t - now for t in self._times]
            for key, curve in self._curves.items():
                curve.setData(xs, list(self._series[key]))

        # Cartões
        cores = tr("{count} núcleos", count=len(snap.cpu_per_core))
        if snap.cpu_freq_current:
            cores += f"  ·  {fmt_number(snap.cpu_freq_current / 1000, 2)} GHz"
        self.card_cpu.set_values(f"{fmt_number(snap.cpu_total)} %", cores)
        self.cpu_bar.set_value(snap.cpu_total)
        self.card_ram.set_values(
            f"{fmt_number(snap.ram_percent)} %",
            tr("{used} de {total}", used=format_bytes(snap.ram_used), total=format_bytes(snap.ram_total)),
        )
        self.ram_bar.set_value(snap.ram_percent)
        self.card_disk.set_values(format_rate(snap.disk_read_bps), tr("Escrita {rate}", rate=format_rate(snap.disk_write_bps)))
        self.card_net.set_values(format_rate(snap.net_down_bps), tr("Upload {rate}", rate=format_rate(snap.net_up_bps)))

        self.chart_cpu.legend.setText(_legend([(tr("Atual"), f"{fmt_number(snap.cpu_total)} %", False)]))
        self.chart_ram.legend.setText(_legend([(tr("Atual"), f"{fmt_number(snap.ram_percent)} %", False)]))
        self.chart_net.legend.setText(_legend([
            (tr("Download"), format_rate(snap.net_down_bps), False),
            (tr("Upload"), format_rate(snap.net_up_bps), True),
        ]))
        self.chart_disk.legend.setText(_legend([
            (tr("Leitura"), format_rate(snap.disk_read_bps), False),
            (tr("Escrita"), format_rate(snap.disk_write_bps), True),
        ]))

        # Núcleos
        self._ensure_core_cells(len(snap.cpu_per_core))
        for cell, value in zip(self._core_cells, snap.cpu_per_core):
            cell.set_value(value)

        # Unidades
        self._ensure_rows(self._disk_rows, self.disks_box, len(snap.disks), DiskRow)
        for row, disk in zip(self._disk_rows, snap.disks):
            row.set_disk(disk)

        # Discos físicos
        rates = sorted(snap.disk_rates.items())
        self._ensure_rows(self._io_labels, self.io_box, max(1, len(rates)), self._io_label)
        if not rates:
            self._io_labels[0].setText("<span style='color:#8a8a8a'>" + tr("Sem dados de velocidade.") + "</span>")
        for label, (name, (read, write)) in zip(self._io_labels, rates):
            label.setText(
                f"<b style='color:#111111'>{name}</b>&nbsp;&nbsp;&nbsp;"
                f"<span style='color:#8a8a8a'>{tr("leitura")}</span> {format_rate(read)}&nbsp;&nbsp;&nbsp;"
                f"<span style='color:#8a8a8a'>{tr("escrita")}</span> {format_rate(write)}"
            )

        # Temperaturas
        if snap.temperatures:
            text = "      ".join(f"{label}  <b>{fmt_number(value)} °C</b>" for label, value in sorted(snap.temperatures.items()))
            self.temps_label.setText(text)
            self.temps_title.show()
            self.temps_label.show()
        else:
            self.temps_title.hide()
            self.temps_label.hide()

    @staticmethod
    def _io_label() -> QLabel:
        label = QLabel()
        label.setObjectName("IoLine")
        label.setTextFormat(Qt.RichText)
        return label
