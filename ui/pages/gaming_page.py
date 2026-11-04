"""Página "Modo Gaming"."""

from __future__ import annotations

import logging
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import gaming, processes
from core.gaming import GamingOptions, GamingReport, RunningApp
from core.processes import ProcessInfo
from core.i18n import tr
from ui import theme
from ui.common import AdminNotice, PageBase, StatusLine, button, confirm, page_header, section_label, show_error
from ui.common import PAGE_MARGINS, PAGE_SPACING
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.gaming")


class GamingPage(PageBase):
    open_settings = Signal()

    def __init__(self, apps: List[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._apps = list(apps)
        self._worker: Optional[TaskWorker] = None
        self._game_worker: Optional[TaskWorker] = None
        self._plan_worker: Optional[TaskWorker] = None
        self._active = gaming.is_active()
        self._pending_options: Optional[GamingOptions] = None
        self._build()
        self._restore_state()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(
            tr("Modo Gaming"),
            tr("Muda para o plano de energia de alto desempenho, fecha apps em segundo plano e pode dar prioridade "
               "ao jogo. Ao desativar, o plano de energia anterior é reposto. Não altera o registo do Windows."),
        ))
        layout.addWidget(AdminNotice(
            tr("Sem privilégios de administrador pode não ser possível alterar a prioridade de alguns jogos.")
        ))

        # Estado + botão principal
        status_card = QFrame()
        status_card.setObjectName("GamingCard")
        card_layout = QHBoxLayout(status_card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        text_col = QVBoxLayout()
        self.state_label = QLabel()
        self.state_label.setObjectName("GamingState")
        self.plan_label = QLabel(tr("Plano de energia atual: a verificar…"))
        self.plan_label.setObjectName("MutedLabel")
        text_col.addWidget(self.state_label)
        text_col.addWidget(self.plan_label)
        card_layout.addLayout(text_col, 1)
        self.toggle_btn = button(tr("Ativar Modo Gaming"), "primary")
        self.toggle_btn.setObjectName("GamingToggle")
        self.toggle_btn.setMinimumHeight(48)
        self.toggle_btn.setMinimumWidth(220)
        self.toggle_btn.clicked.connect(self._toggle)
        card_layout.addWidget(self.toggle_btn, 0, Qt.AlignVCenter)
        layout.addWidget(status_card)

        # Opções
        layout.addWidget(section_label(tr("Opções")))
        options = QFrame()
        options.setObjectName("Card")
        opt_layout = QVBoxLayout(options)
        opt_layout.setContentsMargins(16, 14, 16, 14)
        opt_layout.setSpacing(10)

        self.chk_close = QCheckBox(tr("Fechar apps em segundo plano"))
        self.chk_close.setChecked(True)
        opt_layout.addWidget(self.chk_close)
        apps_row = QHBoxLayout()
        self.apps_label = QLabel()
        self.apps_label.setWordWrap(True)
        self.apps_label.setObjectName("MutedLabel")
        self.apps_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        apps_row.addSpacing(26)
        apps_row.addWidget(self.apps_label, 1)
        edit_btn = button(tr("Editar lista…"))
        edit_btn.clicked.connect(self.open_settings.emit)
        apps_row.addWidget(edit_btn, 0, Qt.AlignTop)
        opt_layout.addLayout(apps_row)

        self.chk_priority = QCheckBox(tr("Definir prioridade Alta para o processo do jogo"))
        self.chk_priority.toggled.connect(self._on_priority_toggled)
        opt_layout.addWidget(self.chk_priority)
        game_row = QHBoxLayout()
        game_row.addSpacing(26)
        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(280)
        self.game_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.game_combo.setEnabled(False)
        game_row.addWidget(self.game_combo, 1)
        self.refresh_games_btn = button(tr("Atualizar lista"), icon_name="atualizar")
        self.refresh_games_btn.setEnabled(False)
        self.refresh_games_btn.clicked.connect(self._load_games)
        game_row.addWidget(self.refresh_games_btn)
        opt_layout.addLayout(game_row)
        hint = QLabel(tr("Abra o jogo primeiro e depois escolha o processo correspondente."))
        hint.setObjectName("MutedLabel")
        hint.setContentsMargins(26, 0, 0, 0)
        opt_layout.addWidget(hint)
        layout.addWidget(options)
        self._options_frame = options

        # Alterações
        layout.addWidget(section_label(tr("Alterações aplicadas")))
        self.changes = QListWidget()
        self.changes.setObjectName("ChangesList")
        self.changes.setMinimumHeight(120)
        layout.addWidget(self.changes, 1)

        self.status = StatusLine()
        layout.addWidget(self.status)
        self.set_apps(self._apps)

    # ------------------------------------------------------------ estado --
    def _restore_state(self) -> None:
        state = gaming.load_state()
        if state.get("active"):
            self._show_changes(state.get("changes", []), state.get("warnings", []),
                               header=tr("Ativo desde {date}", date=state.get('activated_at', '?')))
        self._refresh_ui()
        self._refresh_plan()

    def _refresh_ui(self) -> None:
        if self._active:
            self.state_label.setText(tr("Modo Gaming: ATIVO"))
            self.state_label.setProperty("active", True)
            self.toggle_btn.setText(tr("Desativar Modo Gaming"))
            self.toggle_btn.setProperty("variant", "warning")
        else:
            self.state_label.setText(tr("Modo Gaming: desativado"))
            self.state_label.setProperty("active", False)
            self.toggle_btn.setText(tr("Ativar Modo Gaming"))
            self.toggle_btn.setProperty("variant", "primary")
        for widget in (self.state_label, self.toggle_btn):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._options_frame.setEnabled(not self._active)

    def _refresh_plan(self) -> None:
        if self._plan_worker is not None and self._plan_worker.isRunning():
            return
        self._plan_worker = start_task(gaming.get_active_scheme, on_success=self._on_plan,
                                       on_error=self._on_plan_error)

    def _on_plan(self, scheme) -> None:
        if scheme is None:
            self.plan_label.setText(tr("Plano de energia atual: indisponível"))
        else:
            self.plan_label.setText(tr("Plano de energia atual: {name}", name=scheme.name))

    def _on_plan_error(self, _message: str) -> None:
        self.plan_label.setText(tr("Plano de energia atual: indisponível"))

    def set_apps(self, apps: List[str]) -> None:
        self._apps = list(apps)
        if self._apps:
            self.apps_label.setText(tr("Lista: {apps}", apps=", ".join(self._apps)))
        else:
            self.apps_label.setText(tr("A lista está vazia. Adicione apps em Definições."))

    def is_active(self) -> bool:
        return self._active

    def _on_priority_toggled(self, checked: bool) -> None:
        self.game_combo.setEnabled(checked)
        self.refresh_games_btn.setEnabled(checked)
        if checked and self.game_combo.count() == 0:
            self._load_games()

    def _load_games(self) -> None:
        if self._game_worker is not None and self._game_worker.isRunning():
            return
        self.game_combo.clear()
        self.game_combo.addItem(tr("A carregar processos…"), None)
        self._game_worker = start_task(processes.list_user_processes, on_success=self._on_games,
                                       on_error=self._on_games_error)

    def _on_games(self, procs: List[ProcessInfo]) -> None:
        self.game_combo.clear()
        if not procs:
            self.game_combo.addItem(tr("Nenhum processo encontrado"), None)
            return
        for proc in procs:
            self.game_combo.addItem(tr("{name}  (PID {pid})", name=proc.name, pid=proc.pid), proc)
            self.game_combo.setItemData(self.game_combo.count() - 1, proc.exe, Qt.ToolTipRole)

    def _on_games_error(self, message: str) -> None:
        self.game_combo.clear()
        self.game_combo.addItem(tr("Erro ao carregar processos"), None)
        self.status.set_message(message, "error")

    # ------------------------------------------------------------- ações --
    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.toggle_btn.setEnabled(not busy)
        if message:
            self.status.set_message(message)

    def _toggle(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if self._active:
            self._deactivate()
        else:
            self._prepare_activation()

    def _build_options(self) -> Optional[GamingOptions]:
        options = GamingOptions(close_apps=self.chk_close.isChecked(), apps=list(self._apps))
        if self.chk_priority.isChecked():
            proc = self.game_combo.currentData()
            if not isinstance(proc, ProcessInfo):
                self.status.set_message(tr("Escolha o processo do jogo ou desmarque a opção de prioridade."), "warning")
                return None
            options.game_pid = proc.pid
            options.game_name = proc.name
            options.game_create_time = proc.create_time
        return options

    def _prepare_activation(self) -> None:
        options = self._build_options()
        if options is None:
            return
        self._pending_options = options
        if options.close_apps and options.apps:
            self._set_busy(True, tr("A verificar apps abertas…"))
            self._worker = start_task(gaming.find_running_apps, options.apps,
                                      on_success=self._confirm_activation, on_error=self._on_error)
        else:
            self._confirm_activation([])

    def _confirm_activation(self, running: List[RunningApp]) -> None:
        self._set_busy(False)
        options = self._pending_options
        if options is None:
            return
        lines = [tr("• Mudar o plano de energia para Alto desempenho (ou Desempenho máximo, se existir)")]
        if running:
            names = sorted({app.name for app in running}, key=str.lower)
            lines.append(tr("• Fechar estas apps: {apps}", apps=", ".join(names)))
        elif options.close_apps:
            lines.append(tr("• Nenhuma app da lista está aberta neste momento"))
        if options.game_pid:
            lines.append(tr("• Definir prioridade Alta para {name}", name=options.game_name))
        detail = "\n".join(lines)
        if running:
            detail += "\n\n" + tr("O trabalho não guardado nas apps a fechar será perdido.")
        if not confirm(self, tr("Ativar Modo Gaming"), tr("O Modo Gaming vai fazer o seguinte:"), detail,
                       yes_text=tr("Ativar"), no_text=tr("Cancelar"), danger=bool(running)):
            self.status.set_message(tr("Ativação cancelada."))
            return
        self._set_busy(True, tr("A ativar o Modo Gaming…"))
        self._worker = start_task(gaming.activate, options, on_success=self._on_activated, on_error=self._on_error)

    def _on_activated(self, report: GamingReport) -> None:
        self._set_busy(False)
        self._active = True
        self._show_changes(report.changes, report.warnings, header=tr("Modo Gaming ativado"))
        self._refresh_ui()
        self._refresh_plan()
        kind = "warning" if report.warnings else "success"
        if report.warnings:
            self.status.set_message(tr("Modo Gaming ativado. Alguns passos tiveram avisos."), kind)
        else:
            self.status.set_message(tr("Modo Gaming ativado."), kind)
        self.status_message.emit(tr("Modo Gaming ativado."))

    def _deactivate(self) -> None:
        self._set_busy(True, tr("A desativar o Modo Gaming…"))
        self._worker = start_task(gaming.deactivate, on_success=self._on_deactivated, on_error=self._on_error)

    def _on_deactivated(self, report: GamingReport) -> None:
        self._set_busy(False)
        self._active = False
        self._show_changes(report.changes, report.warnings, header=tr("Modo Gaming desativado"))
        self._refresh_ui()
        self._refresh_plan()
        self.status.set_message(tr("Modo Gaming desativado. Definições anteriores repostas."), "success")
        self.status_message.emit(tr("Modo Gaming desativado."))

    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        self._active = gaming.is_active()
        self._refresh_ui()
        self.status.set_message(message, "error")
        show_error(self, tr("Modo Gaming"), message)

    def _show_changes(self, changes: List[str], warnings: List[str], header: str = "") -> None:
        self.changes.clear()
        if header:
            item = QListWidgetItem(header)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            self.changes.addItem(item)
        for change in changes:
            self.changes.addItem(QListWidgetItem("✔  " + change))
        for warning in warnings:
            item = QListWidgetItem("⚠  " + warning)
            item.setForeground(QColor(theme.c("#c62828")))
            self.changes.addItem(item)

    def deactivate_blocking(self) -> Optional[str]:
        """Desativa de forma síncrona (usado ao fechar a app). Devolve mensagem de erro, se houver."""
        try:
            gaming.deactivate()
            self._active = False
            return None
        except Exception as exc:  # noqa: BLE001
            log.exception("Erro ao desativar o Modo Gaming ao sair")
            return str(exc)

    def shutdown(self) -> None:
        pass
