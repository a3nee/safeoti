"""Página "Arranque": programas que iniciam com o Windows."""

from __future__ import annotations

import logging
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import admin, startup
from core.i18n import tr
from core.processes import ProcessError, open_executable_folder
from core.startup import StartupEntry, StartupPermissionError
from ui import theme
from ui.common import (
    AdminNotice,
    PageBase,
    StatusLine,
    button,
    confirm,
    page_header,
    request_admin_relaunch,
    show_error,
)
from ui.common import PAGE_MARGINS, PAGE_SPACING, style_table
from ui.icons import icon
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.startup")

COL_NAME, COL_STATE, COL_COMMAND, COL_SOURCE = range(4)


class StartupPage(PageBase):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._entries: List[StartupEntry] = []
        self._worker: Optional[TaskWorker] = None
        self._loaded = False
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(
            tr("Arranque"),
            tr("Programas que iniciam com o Windows. Desativar funciona como no Gestor de Tarefas: "
               "a entrada não é apagada e pode ser reativada a qualquer momento."),
        ))
        layout.addWidget(AdminNotice(
            tr("Sem privilégios de administrador só pode alterar entradas do seu utilizador. "
               "As entradas de 'todos os utilizadores' exigem administrador.")
        ))

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Pesquisar por nome ou comando…"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("pesquisa", "#8a8a8a", 16), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        self.btn_refresh = button(tr("Atualizar"), icon_name="atualizar")
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh)
        layout.addLayout(top)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([tr("Nome"), tr("Estado"), tr("Comando / caminho"), tr("Origem")])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        style_table(self.table)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_STATE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_COMMAND, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(COL_NAME, 220)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self._toggle_selected())
        layout.addWidget(self.table, 1)

        self.status = StatusLine()
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.btn_open = button(tr("Abrir localização"), icon_name="pasta")
        self.btn_open.clicked.connect(self._open_location)
        actions.addWidget(self.btn_open)
        actions.addStretch(1)
        self.btn_enable = button(tr("Ativar"), "primary")
        self.btn_disable = button(tr("Desativar"), "warning")
        self.btn_enable.clicked.connect(lambda: self._set_selected(True))
        self.btn_disable.clicked.connect(lambda: self._set_selected(False))
        actions.addWidget(self.btn_enable)
        actions.addWidget(self.btn_disable)
        layout.addLayout(actions)
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    # --------------------------------------------------------------- dados --
    def refresh(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.status.set_message(tr("A ler as entradas de arranque…"))
        self._worker = start_task(startup.list_entries, on_success=self._on_loaded, on_error=self._on_load_error)

    def _on_load_error(self, message: str) -> None:
        self.btn_refresh.setEnabled(True)
        self.status.set_message(message, "error")

    def _on_loaded(self, entries: List[StartupEntry]) -> None:
        self.btn_refresh.setEnabled(True)
        current = self._selected_entry()
        selected_key = current.key if current else None
        self._entries = entries
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(entries))
        is_admin = admin.is_admin()
        for row, entry in enumerate(entries):
            name_item = QTableWidgetItem(entry.display_name)
            name_item.setData(Qt.UserRole, row)
            name_item.setToolTip(entry.name)
            state_item = QTableWidgetItem()
            command_item = QTableWidgetItem(entry.command)
            command_item.setToolTip(entry.command)
            source_text = entry.source.label
            if entry.requires_admin and not is_admin:
                source_text += "  🔒"
            source_item = QTableWidgetItem(source_text)
            source_item.setToolTip(
                f"{entry.source.hive_name}\\{entry.source.location}" if entry.source.kind == "registry" else entry.source.location
            )
            if entry.requires_admin and not is_admin:
                source_item.setToolTip(source_item.toolTip() + "\n" + tr("Alterar esta entrada exige privilégios de administrador."))
            self.table.setItem(row, COL_NAME, name_item)
            self.table.setItem(row, COL_STATE, state_item)
            self.table.setItem(row, COL_COMMAND, command_item)
            self.table.setItem(row, COL_SOURCE, source_item)
            self._paint_state(row, entry)
        self.table.setSortingEnabled(True)
        enabled = sum(1 for e in entries if e.enabled)
        self.status.set_message(tr(
            "{total} entradas encontradas ({enabled} ativas, {disabled} desativadas).",
            total=len(entries), enabled=enabled, disabled=len(entries) - enabled,
        ))
        self._apply_filter(self.search.text())
        if selected_key:
            for row in range(self.table.rowCount()):
                entry = self._entry_at(row)
                if entry and entry.key == selected_key:
                    self.table.selectRow(row)
                    break
        self._update_buttons()

    def _paint_state(self, row: int, entry: StartupEntry) -> None:
        item = self.table.item(row, COL_STATE)
        if item is None:
            return
        item.setText(tr("Ativo") if entry.enabled else tr("Desativado"))
        item.setForeground(QColor(theme.c("#000000" if entry.enabled else "#9a9a9a")))
        font = item.font()
        font.setBold(entry.enabled)
        item.setFont(font)

    def _entry_at(self, row: int) -> Optional[StartupEntry]:
        item = self.table.item(row, COL_NAME)
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        if index is None or not (0 <= index < len(self._entries)):
            return None
        return self._entries[index]

    def _selected_entry(self) -> Optional[StartupEntry]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._entry_at(rows[0].row()) if rows else None

    def _selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.table.rowCount()):
            entry = self._entry_at(row)
            visible = True
            if needle and entry:
                visible = needle in entry.display_name.lower() or needle in entry.command.lower()
            self.table.setRowHidden(row, not visible)

    def _update_buttons(self) -> None:
        entry = self._selected_entry()
        self.btn_enable.setEnabled(bool(entry and not entry.enabled))
        self.btn_disable.setEnabled(bool(entry and entry.enabled))
        self.btn_open.setEnabled(entry is not None)
        if entry and entry.requires_admin and not admin.is_admin():
            tip = tr("Esta entrada aplica-se a todos os utilizadores: alterar exige privilégios de administrador.")
        else:
            tip = ""
        self.btn_enable.setToolTip(tip)
        self.btn_disable.setToolTip(tip)

    # -------------------------------------------------------------- ações --
    def _toggle_selected(self) -> None:
        entry = self._selected_entry()
        if entry:
            self._set_selected(not entry.enabled)

    def _set_selected(self, enabled: bool) -> None:
        entry = self._selected_entry()
        row = self._selected_row()
        if entry is None or entry.enabled == enabled:
            return
        if entry.requires_admin and not admin.is_admin():
            if confirm(
                self,
                tr("Privilégios de administrador"),
                tr("'{name}' aplica-se a todos os utilizadores e só pode ser alterado como administrador.",
                   name=entry.display_name),
                tr("Quer reiniciar o SafeOti como administrador?"),
                yes_text=tr("Reiniciar como administrador"),
                no_text=tr("Cancelar"),
                danger=False,
            ):
                request_admin_relaunch(self)
            return
        try:
            startup.set_enabled(entry, enabled)
        except StartupPermissionError as exc:
            show_error(self, tr("Arranque"), str(exc), tr("Reinicie o SafeOti como administrador e tente novamente."))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Erro ao alterar entrada de arranque")
            show_error(self, tr("Arranque"), tr("Não foi possível alterar '{name}'.", name=entry.display_name), str(exc))
            return
        self._paint_state(row, entry)
        if enabled:
            message = tr("'{name}' foi ativado. A alteração tem efeito no próximo arranque do Windows.",
                         name=entry.display_name)
        else:
            message = tr("'{name}' foi desativado. A alteração tem efeito no próximo arranque do Windows.",
                         name=entry.display_name)
        self.status.set_message(message, "success")
        self.status_message.emit(message)
        self._update_buttons()

    def _open_location(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        path = startup.entry_location(entry)
        if not path:
            show_error(self, tr("Arranque"), tr("Não foi possível determinar a localização deste programa."), entry.command)
            return
        try:
            open_executable_folder(path)
        except ProcessError as exc:
            show_error(self, tr("Arranque"), str(exc))
        except OSError as exc:
            show_error(self, tr("Arranque"), tr("Não foi possível abrir o Explorador."), str(exc))

    def shutdown(self) -> None:
        pass

