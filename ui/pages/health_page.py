"""Página "Saúde": estado dos discos (SMART/fiabilidade) e da bateria."""

from __future__ import annotations

import logging
from typing import List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core import health
from core.health import BatteryInfo, BatteryReport, DiskHealth, DiskReport, HealthSnapshot, HtmlReportResult
from core.i18n import tr
from core.utils import format_bytes
from ui.common import (
    PAGE_MARGINS,
    PAGE_SPACING,
    AdminNotice,
    PageBase,
    StatusLine,
    bold_font,
    button,
    fmt_number,
    page_header,
    section_label,
    show_error,
)
from ui.icons import pixmap
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.health")

DASH = "—"

# Veredicto -> tipo da StatusLine (cores definidas em styles.qss / tema).
_VERDICT_KIND = {
    health.VERDICT_OK: "success",
    health.VERDICT_WARNING: "warning",
    health.VERDICT_CRITICAL: "error",
    health.VERDICT_UNKNOWN: "info",
}


def verdict_text(verdict: str) -> str:
    if verdict == health.VERDICT_OK:
        return tr("Bom estado")
    if verdict == health.VERDICT_WARNING:
        return tr("Atenção")
    if verdict == health.VERDICT_CRITICAL:
        return tr("Crítico")
    return tr("Estado desconhecido")


def _kind_text(disk: DiskHealth) -> str:
    kind = disk.kind
    if kind == "NVMe":
        return tr("SSD NVMe")
    if kind:
        return kind
    return tr("Tipo desconhecido")


def _value_or_dash(value, fmt) -> str:
    return DASH if value is None else fmt(value)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


class _InfoGrid(QGridLayout):
    """Grelha de pares título/valor em colunas."""

    def __init__(self, columns: int = 3) -> None:
        super().__init__()
        self._columns = columns
        self._count = 0
        self.setHorizontalSpacing(24)
        self.setVerticalSpacing(10)

    def add(self, title: str, value: str, tooltip: str = "") -> None:
        box = QVBoxLayout()
        box.setSpacing(2)
        label = QLabel(title)
        label.setObjectName("CardTitle")
        val = QLabel(value)
        val.setFont(bold_font(val, 1))
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if tooltip:
            val.setToolTip(tooltip)
        box.addWidget(label)
        box.addWidget(val)
        row, col = divmod(self._count, self._columns)
        self.addLayout(box, row, col)
        self.setColumnStretch(col, 1)
        self._count += 1


def _card_header(icon_name: str, title: str, subtitle: str, verdict: str) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(12)
    icon_label = QLabel()
    icon_label.setPixmap(pixmap(icon_name, size=22))
    icon_label.setFixedSize(26, 26)
    row.addWidget(icon_label, 0, Qt.AlignTop)

    texts = QVBoxLayout()
    texts.setSpacing(2)
    name = QLabel(title)
    name.setFont(bold_font(name, 2))
    name.setWordWrap(True)
    name.setTextInteractionFlags(Qt.TextSelectableByMouse)
    texts.addWidget(name)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("CardSubtitle")
        sub.setWordWrap(True)
        sub.setTextInteractionFlags(Qt.TextSelectableByMouse)
        texts.addWidget(sub)
    row.addLayout(texts, 1)

    status = StatusLine()
    status.set_message(verdict_text(verdict), _VERDICT_KIND.get(verdict, "info"))
    status.setWordWrap(False)
    row.addWidget(status, 0, Qt.AlignTop)
    return row


def _reasons_label(reasons: List[str]) -> Optional[QLabel]:
    if not reasons:
        return None
    label = QLabel("\n".join(f"• {r}" for r in reasons))
    label.setObjectName("MutedLabel")
    label.setWordWrap(True)
    return label


class DiskCard(QFrame):
    def __init__(self, disk: DiskHealth, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        parts = [_kind_text(disk)]
        if disk.bus_type and disk.kind != "NVMe":
            parts.append(disk.bus_type)
        if disk.size:
            parts.append(format_bytes(disk.size))
        if disk.serial:
            parts.append(tr("N.º de série {serial}", serial=disk.serial))
        layout.addLayout(_card_header("disco", disk.name or tr("Disco {id}", id=disk.device_id),
                                      " · ".join(parts), disk.verdict))

        grid = _InfoGrid(3)
        grid.add(tr("Estado no Windows"), self._health_text(disk))
        temp = _value_or_dash(disk.temperature, lambda v: tr("{temp} °C", temp=v))
        temp_tip = ""
        if disk.temperature_max is not None:
            temp_tip = tr("Máxima registada: {temp} °C", temp=disk.temperature_max)
        grid.add(tr("Temperatura"), temp, temp_tip)
        grid.add(tr("Desgaste"), _value_or_dash(disk.wear, lambda v: f"{v}%"))
        grid.add(tr("Horas ligado"), _value_or_dash(disk.power_on_hours, lambda v: tr("{hours} h", hours=v)))
        if disk.read_errors is None and disk.write_errors is None:
            errors = DASH
        else:
            errors = tr("{read} leitura · {write} escrita",
                        read=_value_or_dash(disk.read_errors, str), write=_value_or_dash(disk.write_errors, str))
        grid.add(tr("Erros"), errors)
        grid.add(tr("Previsão de falha (SMART)"), self._predict_text(disk.predict_failure))
        layout.addLayout(grid)

        reasons = _reasons_label(disk.reasons)
        if reasons is not None:
            layout.addWidget(reasons)
        if disk.needs_admin:
            hint = QLabel(tr("Temperatura, desgaste e erros precisam de administrador."))
            hint.setObjectName("MutedLabel")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        elif not disk.reliability_available:
            hint = QLabel(tr("Este disco não disponibiliza contadores de fiabilidade."))
            hint.setObjectName("MutedLabel")
            hint.setWordWrap(True)
            layout.addWidget(hint)

    @staticmethod
    def _health_text(disk: DiskHealth) -> str:
        status = disk.health_status.lower()
        if status == "healthy":
            return tr("Saudável")
        if status == "warning":
            return tr("Aviso")
        if status == "unhealthy":
            return tr("Não saudável")
        if not status or status == "unknown":
            return DASH
        return disk.health_status

    @staticmethod
    def _predict_text(value: Optional[bool]) -> str:
        if value is None:
            return DASH
        return tr("Falha prevista") if value else tr("Sem falhas previstas")


class BatteryCard(QFrame):
    def __init__(self, battery: BatteryInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        parts = [p for p in (battery.manufacturer, battery.chemistry) if p]
        title = battery.id or tr("Bateria")
        layout.addLayout(_card_header("bateria", title, " · ".join(parts), battery.verdict))

        grid = _InfoGrid(3)
        grid.add(tr("Capacidade de fábrica"), _mwh(battery.design_capacity))
        grid.add(tr("Capacidade atual (carga completa)"), _mwh(battery.full_charge_capacity))
        health_pct = battery.health_percent
        grid.add(tr("Saúde"), _value_or_dash(health_pct, lambda v: f"{fmt_number(v, 1)}%"))
        grid.add(tr("Desgaste"), _value_or_dash(battery.wear_percent, lambda v: f"{fmt_number(v, 1)}%"))
        grid.add(tr("Ciclos de carga"), _value_or_dash(battery.cycle_count, str),
                 tr("Alguns fabricantes não reportam ciclos (aparece 0)."))
        layout.addLayout(grid)

        reasons = _reasons_label(battery.reasons)
        if reasons is not None:
            layout.addWidget(reasons)


def _mwh(value: Optional[int]) -> str:
    if value is None:
        return DASH
    return tr("{value} Wh", value=fmt_number(value / 1000, 1))


class HealthPage(PageBase):
    """Saúde dos discos e da bateria. Carrega na primeira vez que é mostrada."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._worker: Optional[TaskWorker] = None
        self._report_worker: Optional[TaskWorker] = None
        self._loaded = False
        self.snapshot: Optional[HealthSnapshot] = None
        self._build()

    # ------------------------------------------------------------- interface --
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

        top = QHBoxLayout()
        top.addWidget(page_header(
            tr("Saúde"),
            tr("Estado dos discos e da bateria, segundo o Windows. Apenas leitura: nada é alterado."),
        ), 1)
        self.btn_refresh = button(tr("Atualizar"), icon_name="atualizar")
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh, 0, Qt.AlignTop)
        layout.addLayout(top)

        # Discos
        layout.addWidget(section_label(tr("Discos")))
        self.disk_admin_notice = AdminNotice(
            tr("A temperatura, o desgaste, as horas de funcionamento e os erros dos discos "
               "só podem ser lidos como administrador.")
        )
        self.disk_admin_notice.setVisible(False)
        layout.addWidget(self.disk_admin_notice)
        self.disks_box = QVBoxLayout()
        self.disks_box.setSpacing(12)
        layout.addLayout(self.disks_box)
        self.disks_message = QLabel(tr("A carregar…"))
        self.disks_message.setObjectName("MutedLabel")
        self.disks_message.setWordWrap(True)
        layout.addWidget(self.disks_message)

        # Bateria
        battery_row = QHBoxLayout()
        battery_row.addWidget(section_label(tr("Bateria")))
        battery_row.addStretch(1)
        self.btn_report = button(
            tr("Relatório completo"), variant="link",
            tooltip=tr("Gera o relatório de bateria do Windows (HTML) e abre-o no navegador."),
        )
        self.btn_report.clicked.connect(self._open_report)
        self.btn_report.setVisible(False)
        battery_row.addWidget(self.btn_report)
        layout.addLayout(battery_row)
        self.battery_box = QVBoxLayout()
        self.battery_box.setSpacing(12)
        layout.addLayout(self.battery_box)
        self.battery_message = QLabel(tr("A carregar…"))
        self.battery_message.setObjectName("MutedLabel")
        self.battery_message.setWordWrap(True)
        layout.addWidget(self.battery_message)

        layout.addStretch(1)
        self.status = StatusLine()
        layout.addWidget(self.status)

    def showEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    # ----------------------------------------------------------------- dados --
    def refresh(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._loaded = True
        self.btn_refresh.setEnabled(False)
        self.status.set_message(tr("A verificar discos e bateria…"))
        self._worker = start_task(health.collect, on_success=self._on_loaded, on_error=self._on_error)

    def _on_error(self, message: str) -> None:
        self.btn_refresh.setEnabled(True)
        self.status.set_message(message, "error")
        self.disks_message.setText(DASH)
        self.battery_message.setText(DASH)

    def _on_loaded(self, snapshot: HealthSnapshot) -> None:
        self.btn_refresh.setEnabled(True)
        self.snapshot = snapshot
        self._show_disks(snapshot.disks)
        self._show_battery(snapshot.battery)
        verdict = health.worst(
            *(d.verdict for d in snapshot.disks.disks),
            snapshot.battery.verdict,
        )
        if verdict == health.VERDICT_CRITICAL:
            self.status.set_message(tr("Foram encontrados problemas graves. Veja os detalhes acima."), "error")
        elif verdict == health.VERDICT_WARNING:
            self.status.set_message(tr("Há pontos que merecem atenção."), "warning")
        else:
            self.status.set_message(tr("Verificação concluída."), "success")
        self.status_message.emit(tr("Saúde do sistema verificada."))

    def _show_disks(self, report: DiskReport) -> None:
        _clear_layout(self.disks_box)
        self.disk_admin_notice.setVisible(report.needs_admin)
        for disk in report.disks:
            self.disks_box.addWidget(DiskCard(disk))
        if report.disks:
            self.disks_message.setVisible(False)
        else:
            self.disks_message.setText(report.error or tr("Não foram encontrados discos."))
            self.disks_message.setVisible(True)

    def _show_battery(self, report: BatteryReport) -> None:
        _clear_layout(self.battery_box)
        for battery in report.batteries:
            self.battery_box.addWidget(BatteryCard(battery))
        self.btn_report.setVisible(report.present)
        if report.batteries:
            self.battery_message.setVisible(False)
            return
        if report.error:
            text = report.error
        else:
            text = tr("Sem bateria: este computador não tem bateria ou o Windows não a deteta.")
        self.battery_message.setText(text)
        self.battery_message.setVisible(True)

    # ------------------------------------------------------------- relatório --
    def _open_report(self) -> None:
        if self._report_worker is not None and self._report_worker.isRunning():
            return
        self.btn_report.setEnabled(False)
        self.status.set_message(tr("A gerar o relatório da bateria…"))
        self._report_worker = start_task(
            health.generate_battery_html_report,
            on_success=self._on_report,
            on_error=self._on_report_error,
        )

    def _on_report(self, result: HtmlReportResult) -> None:
        self.btn_report.setEnabled(True)
        if not result.ok:
            self._on_report_error(result.message)
            return
        self.status.set_message(tr("Relatório guardado em {path}", path=result.path), "success")
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(result.path)):
            show_error(self, tr("Relatório da bateria"), tr("Não foi possível abrir o relatório."), result.path)

    def _on_report_error(self, message: str) -> None:
        self.btn_report.setEnabled(True)
        self.status.set_message(message, "error")
        show_error(self, tr("Relatório da bateria"), tr("Não foi possível gerar o relatório da bateria."), message)

    def shutdown(self) -> None:
        """Chamado pela janela principal ao fechar: as tarefas são curtas e
        terminam sozinhas (``workers.stop_all`` espera por elas)."""
        for worker in (self._worker, self._report_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
