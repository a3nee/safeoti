"""Página "Processos": lista de processos em execução."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core import processes
from core.i18n import tr
from core.processes import ProcessError, ProcessInfo
from core.utils import format_bytes
from ui.common import AdminNotice, PageBase, StatusLine, button, confirm, fmt_number, page_header, show_error, show_warning
from ui.common import PAGE_MARGINS, PAGE_SPACING, style_table
from ui.icons import icon
from ui.workers import ProcessWorker, TaskWorker, start_task

log = logging.getLogger("safeoti.ui.processes")

COL_NAME, COL_PID, COL_CPU, COL_MEM, COL_USER = range(5)
HEADERS = ["Nome", "PID", "CPU %", "Memória", "Utilizador"]
SORT_ROLE = Qt.UserRole + 1

ProcessKey = Tuple[int, float]


class ProcessTableModel(QAbstractTableModel):
    """Modelo atualizado de forma incremental, para não perder seleção nem scroll."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[ProcessInfo] = []
        self._index: Dict[ProcessKey, int] = {}

    # API do modelo
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return tr(HEADERS[section])
        if orientation == Qt.Horizontal and role == Qt.TextAlignmentRole:
            if section in (COL_PID, COL_CPU, COL_MEM):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        proc = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return proc.name
            if col == COL_PID:
                return str(proc.pid)
            if col == COL_CPU:
                return fmt_number(proc.cpu_percent)
            if col == COL_MEM:
                return format_bytes(proc.memory_bytes)
            if col == COL_USER:
                return proc.username or "—"
        elif role == SORT_ROLE:
            if col == COL_NAME:
                return proc.name.lower()
            if col == COL_PID:
                return proc.pid
            if col == COL_CPU:
                return proc.cpu_percent
            if col == COL_MEM:
                return proc.memory_bytes
            if col == COL_USER:
                return (proc.username or "").lower()
        elif role == Qt.TextAlignmentRole and col in (COL_PID, COL_CPU, COL_MEM):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ToolTipRole and col == COL_NAME:
            return proc.exe or proc.name
        return None

    def process_at(self, row: int) -> Optional[ProcessInfo]:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    @staticmethod
    def _key(proc: ProcessInfo) -> ProcessKey:
        return (proc.pid, round(proc.create_time, 3))

    def update(self, new_rows: List[ProcessInfo]) -> None:
        new_by_key = {self._key(p): p for p in new_rows}

        # 1) Remover processos que terminaram (de baixo para cima).
        gone = sorted((row for key, row in self._index.items() if key not in new_by_key), reverse=True)
        for row in gone:
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._rows[row]
            self.endRemoveRows()
        if gone:
            self._index = {self._key(p): i for i, p in enumerate(self._rows)}

        # 2) Atualizar os existentes no lugar.
        for i, proc in enumerate(self._rows):
            fresh = new_by_key.get(self._key(proc))
            if fresh is not None:
                self._rows[i] = fresh
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, len(HEADERS) - 1))

        # 3) Acrescentar os novos no fim.
        added = [p for key, p in new_by_key.items() if key not in self._index]
        if added:
            start = len(self._rows)
            self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
            self._rows.extend(added)
            self.endInsertRows()
            for offset, proc in enumerate(added):
                self._index[self._key(proc)] = start + offset


class ProcessesPage(PageBase):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._task: Optional[TaskWorker] = None
        self.worker = ProcessWorker(2000)
        self.worker.processes.connect(self._on_processes)
        self.worker.failed.connect(self._on_worker_failed)
        self._build()
        self.worker.start()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(tr("Processos"), tr("Processos em execução, atualizados a cada 2 segundos.")))
        layout.addWidget(AdminNotice(
            tr("Sem privilégios de administrador não é possível terminar processos de outros utilizadores ou do sistema.")
        ))

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Pesquisar por nome…"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("pesquisa", "#8a8a8a", 16), QLineEdit.LeadingPosition)
        top.addWidget(self.search, 1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("MutedLabel")
        top.addWidget(self.count_label)
        layout.addLayout(top)

        self.model = ProcessTableModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setFilterKeyColumn(COL_NAME)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setDynamicSortFilter(True)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(COL_CPU, Qt.DescendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        style_table(self.view)
        self.view.setWordWrap(False)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for col in (COL_PID, COL_CPU, COL_MEM):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            self.view.setColumnWidth(col, 100)
        header.setSectionResizeMode(COL_USER, QHeaderView.Interactive)
        self.view.setColumnWidth(COL_USER, 160)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        layout.addWidget(self.view, 1)

        self.status = StatusLine()
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.btn_folder = button(tr("Abrir pasta do executável"), icon_name="pasta")
        self.btn_folder.clicked.connect(self._open_folder)
        actions.addWidget(self.btn_folder)
        actions.addStretch(1)
        self.btn_kill = button(tr("Terminar processo"), "danger")
        self.btn_kill.clicked.connect(self._terminate)
        actions.addWidget(self.btn_kill)
        layout.addLayout(actions)
        self._update_buttons()

    # ---------------------------------------------------- visibilidade --
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.worker.set_active(True)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.worker.set_active(False)

    # ------------------------------------------------------------ dados --
    def _on_processes(self, rows: List[ProcessInfo]) -> None:
        scroll = self.view.verticalScrollBar().value()
        self.model.update(rows)
        self.view.verticalScrollBar().setValue(scroll)
        visible = self.proxy.rowCount()
        total = self.model.rowCount()
        self.count_label.setText(
            tr("{visible} de {total} processos", visible=visible, total=total)
            if visible != total else tr("{total} processos", total=total)
        )
        self._update_buttons()

    def _on_worker_failed(self, message: str) -> None:
        self.status.set_message(message, "error")

    def selected_process(self) -> Optional[ProcessInfo]:
        rows = self.view.selectionModel().selectedRows()
        if not rows:
            return None
        source = self.proxy.mapToSource(rows[0])
        return self.model.process_at(source.row())

    def _update_buttons(self) -> None:
        proc = self.selected_process()
        busy = self._task is not None and self._task.isRunning()
        self.btn_kill.setEnabled(proc is not None and not busy)
        self.btn_folder.setEnabled(bool(proc and proc.exe))
        if proc is not None and processes.is_critical(proc.name, proc.pid):
            self.btn_kill.setToolTip(tr("Processo essencial do Windows: não pode ser terminado."))
        else:
            self.btn_kill.setToolTip("")

    def _context_menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        self.view.selectRow(index.row())
        proc = self.selected_process()
        if proc is None:
            return
        menu = QMenu(self)
        act_folder = menu.addAction(tr("Abrir pasta do executável"))
        act_folder.setEnabled(bool(proc.exe))
        act_kill = menu.addAction(tr("Terminar processo"))
        chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
        if chosen is act_folder:
            self._open_folder()
        elif chosen is act_kill:
            self._terminate()

    # ------------------------------------------------------------ ações --
    def _terminate(self) -> None:
        proc = self.selected_process()
        if proc is None:
            return
        if processes.is_critical(proc.name, proc.pid):
            show_warning(self, tr("Processo protegido"), processes.critical_reason(proc.name, proc.pid))
            log.info("Tentativa bloqueada de terminar %s (PID %d)", proc.name, proc.pid)
            return
        warning = processes.warning_for(proc.name)
        if warning:
            if not confirm(self, tr("Atenção"), tr("Tem a certeza de que quer terminar '{name}'?", name=proc.name), warning,
                           yes_text=tr("Terminar mesmo assim"), no_text=tr("Cancelar")):
                return
        elif not confirm(
            self,
            tr("Terminar processo"),
            tr("Terminar '{name}' (PID {pid})?", name=proc.name, pid=proc.pid),
            tr("O trabalho não guardado nesse programa será perdido."),
            yes_text=tr("Terminar"),
            no_text=tr("Cancelar"),
        ):
            return
        self.status.set_message(tr("A terminar {name}…", name=proc.name))
        self._task = start_task(
            processes.terminate_process, proc.pid, proc.name, proc.create_time,
            on_success=self._on_terminated, on_error=self._on_terminate_error,
        )
        self._update_buttons()

    def _on_terminated(self, message: str) -> None:
        self.status.set_message(message, "success")
        self.status_message.emit(message)
        self.worker.refresh_now()
        self._update_buttons()

    def _on_terminate_error(self, message: str) -> None:
        self.status.set_message(message, "error")
        show_error(self, tr("Terminar processo"), message)
        self._update_buttons()

    def _open_folder(self) -> None:
        proc = self.selected_process()
        if proc is None:
            return
        try:
            processes.open_executable_folder(proc.exe)
        except ProcessError as exc:
            show_error(self, tr("Abrir pasta"), str(exc))
        except OSError as exc:
            show_error(self, tr("Abrir pasta"), tr("Não foi possível abrir o Explorador."), str(exc))

    def shutdown(self) -> None:
        self.worker.stop()
