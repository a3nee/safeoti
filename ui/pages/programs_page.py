"""Página "Programas": programas instalados e desinstalação."""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from PySide6.QtCore import QUrl, QUrlQuery, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import admin, programs
from core.i18n import tr
from core.programs import Program, ProgramsError, ProgramsPermissionError, UninstallResult
from core.utils import format_bytes
from ui.common import (
    AdminNotice,
    PageBase,
    StatusLine,
    button,
    confirm,
    page_header,
    request_admin_relaunch,
    show_error,
    show_info,
)
from ui.common import PAGE_MARGINS, PAGE_SPACING, style_table
from ui.icons import icon
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.programs")

COL_NAME, COL_PUBLISHER, COL_VERSION, COL_DATE, COL_SIZE, COL_SOURCE = range(6)
SORT_ROLE = Qt.UserRole + 1


class _SortItem(QTableWidgetItem):
    """Item que ordena pelo valor guardado em SORT_ROLE (datas e tamanhos)."""

    def __lt__(self, other: QTableWidgetItem) -> bool:  # noqa: D105
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is not None and theirs is not None:
            return mine < theirs
        return super().__lt__(other)


def _run_uninstall(program: Program, allow_self: bool, wait: bool, progress, should_cancel) -> Tuple[str, object]:
    """Executa a desinstalação e devolve ("ok", resultado), ("admin", msg) ou ("error", msg).

    Os erros esperados são devolvidos (em vez de lançados) para a mensagem
    chegar intacta ao utilizador.
    """
    try:
        return "ok", programs.uninstall(program, allow_self=allow_self, wait=wait,
                                        progress=progress, should_cancel=should_cancel)
    except ProgramsPermissionError as exc:
        return "admin", str(exc)
    except ProgramsError as exc:
        return "error", str(exc)


def _run_list(include_store: bool) -> Tuple[str, object]:
    try:
        return "ok", programs.list_programs(include_store)
    except ProgramsError as exc:
        return "error", str(exc)


class ProgramsPage(PageBase):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._programs: List[Program] = []
        self._worker: Optional[TaskWorker] = None
        self._uninstall_worker: Optional[TaskWorker] = None
        self._loaded = False
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(
            tr("Programas"),
            tr("Programas instalados neste PC. Desinstalar abre o desinstalador do próprio programa, "
               "onde pode confirmar ou cancelar."),
        ))
        layout.addWidget(AdminNotice(
            tr("Sem privilégios de administrador só pode desinstalar programas do seu utilizador e "
               "aplicações da Microsoft Store.")
        ))

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Pesquisar por nome ou editor…"))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("pesquisa", "#8a8a8a", 16), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        self.chk_store = QCheckBox(tr("Mostrar aplicações da Microsoft Store"))
        self.chk_store.setChecked(True)
        self.chk_store.toggled.connect(lambda _checked: self.refresh())
        top.addWidget(self.chk_store)
        self.btn_refresh = button(tr("Atualizar"), icon_name="atualizar")
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh)
        layout.addLayout(top)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            tr("Nome"), tr("Editor"), tr("Versão"), tr("Instalado em"), tr("Tamanho"), tr("Origem"),
        ])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        style_table(self.table)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_NAME, Qt.AscendingOrder)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_PUBLISHER, QHeaderView.Interactive)
        for col in (COL_VERSION, COL_DATE, COL_SIZE, COL_SOURCE):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(COL_PUBLISHER, 200)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.status = StatusLine()
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.btn_open = button(tr("Abrir localização"), icon_name="pasta")
        self.btn_open.clicked.connect(self._open_location)
        actions.addWidget(self.btn_open)
        self.btn_web = button(tr("Procurar na Internet"), icon_name="pesquisa")
        self.btn_web.clicked.connect(self._search_web)
        actions.addWidget(self.btn_web)
        actions.addStretch(1)
        self.btn_uninstall = button(tr("Desinstalar"), "danger")
        self.btn_uninstall.clicked.connect(self._uninstall_selected)
        actions.addWidget(self.btn_uninstall)
        layout.addLayout(actions)
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    # --------------------------------------------------------------- dados --
    def _busy(self) -> bool:
        return self._uninstall_worker is not None and self._uninstall_worker.isRunning()

    def refresh(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.chk_store.setEnabled(False)
        if not self._busy():
            self.status.set_message(tr("A ler os programas instalados…"))
        self._worker = start_task(_run_list, self.chk_store.isChecked(),
                                  on_success=self._on_loaded, on_error=self._on_load_error)

    def _on_load_error(self, message: str) -> None:
        self.btn_refresh.setEnabled(True)
        self.chk_store.setEnabled(True)
        self.status.set_message(message, "error")

    def _on_loaded(self, outcome: Tuple[str, object]) -> None:
        kind, payload = outcome
        if kind != "ok":
            self._on_load_error(str(payload))
            return
        self.btn_refresh.setEnabled(True)
        self.chk_store.setEnabled(True)
        current = self._selected_program()
        selected_uid = current.uid if current else None
        self._programs = list(payload)  # type: ignore[arg-type]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._programs))
        for row, program in enumerate(self._programs):
            name_item = QTableWidgetItem(program.name)
            name_item.setData(Qt.UserRole, row)
            name_item.setToolTip(program.name)
            self.table.setItem(row, COL_NAME, name_item)
            self.table.setItem(row, COL_PUBLISHER, QTableWidgetItem(program.publisher))
            self.table.setItem(row, COL_VERSION, QTableWidgetItem(program.version))

            date_item = _SortItem(program.install_date.strftime("%Y-%m-%d") if program.install_date else "")
            date_item.setData(SORT_ROLE, program.install_date.toordinal() if program.install_date else 0)
            self.table.setItem(row, COL_DATE, date_item)

            size_item = _SortItem(format_bytes(program.size_bytes) if program.size_bytes else "")
            size_item.setData(SORT_ROLE, program.size_bytes)
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, COL_SIZE, size_item)

            source_item = QTableWidgetItem(self._source_label(program))
            source_item.setToolTip(program.package_full_name if program.is_store else program.key_path)
            self.table.setItem(row, COL_SOURCE, source_item)
        self.table.setSortingEnabled(True)
        if not self._busy():
            stores = sum(1 for p in self._programs if p.is_store)
            self.status.set_message(tr("{total} programas encontrados ({store} da Microsoft Store).",
                                       total=len(self._programs), store=stores))
        self._apply_filter(self.search.text())
        if selected_uid is not None:
            for row in range(self.table.rowCount()):
                program = self._program_at(row)
                if program is not None and program.uid == selected_uid:
                    self.table.selectRow(row)
                    break
        self._update_buttons()

    @staticmethod
    def _source_label(program: Program) -> str:
        if program.is_store:
            return tr("Microsoft Store")
        if program.hive == "HKCU":
            return tr("Utilizador atual")
        return tr("Todos os utilizadores")

    def _program_at(self, row: int) -> Optional[Program]:
        item = self.table.item(row, COL_NAME)
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        if index is None or not (0 <= index < len(self._programs)):
            return None
        return self._programs[index]

    def _selected_program(self) -> Optional[Program]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._program_at(rows[0].row()) if rows else None

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in range(self.table.rowCount()):
            program = self._program_at(row)
            visible = True
            if needle and program is not None:
                visible = needle in program.name.lower() or needle in program.publisher.lower()
            self.table.setRowHidden(row, not visible)

    def _update_buttons(self) -> None:
        program = self._selected_program()
        self.btn_open.setEnabled(program is not None and bool(self._location(program)))
        self.btn_web.setEnabled(program is not None)
        self.btn_uninstall.setEnabled(program is not None and not self._busy())
        tip = ""
        if program is not None and program.requires_admin and not admin.is_admin():
            tip = tr("Este programa foi instalado para todos os utilizadores: desinstalar exige privilégios "
                     "de administrador.")
        self.btn_uninstall.setToolTip(tip)

    @staticmethod
    def _location(program: Program) -> str:
        if program.is_store:
            return program.install_location if os.path.isdir(program.install_location) else ""
        return programs.program_location(program)

    # ------------------------------------------------------------ menu --
    def _context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        self.table.selectRow(item.row())
        program = self._selected_program()
        if program is None:
            return
        menu = QMenu(self)
        act_uninstall = menu.addAction(tr("Desinstalar"))
        act_uninstall.setEnabled(not self._busy())
        menu.addSeparator()
        act_open = menu.addAction(tr("Abrir localização"))
        act_open.setEnabled(bool(self._location(program)))
        act_web = menu.addAction(tr("Procurar na Internet"))
        act_copy = menu.addAction(tr("Copiar nome"))
        act_copy_cmd = None
        if not program.is_store:
            act_copy_cmd = menu.addAction(tr("Copiar comando de desinstalação"))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_uninstall:
            self._uninstall_selected()
        elif chosen is act_open:
            self._open_location()
        elif chosen is act_web:
            self._search_web()
        elif chosen is act_copy:
            QApplication.clipboard().setText(program.name)
            self.status.set_message(tr("Nome copiado."), "success")
        elif chosen is act_copy_cmd:
            QApplication.clipboard().setText(program.uninstall_string)
            self.status.set_message(tr("Comando copiado."), "success")

    # ------------------------------------------------------------ ações --
    def _open_location(self) -> None:
        program = self._selected_program()
        if program is None:
            return
        path = self._location(program)
        if not path:
            show_error(self, tr("Programas"), tr("Não foi possível determinar a pasta de '{name}'.", name=program.name))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            show_error(self, tr("Programas"), tr("Não foi possível abrir o Explorador."), path)

    def _search_web(self) -> None:
        program = self._selected_program()
        if program is None:
            return
        url = QUrl("https://www.bing.com/search")
        query = QUrlQuery()
        query.addQueryItem("q", f"{program.name} {program.publisher}".strip())
        url.setQuery(query)
        QDesktopServices.openUrl(url)

    def _uninstall_selected(self) -> None:
        program = self._selected_program()
        if program is None or self._busy():
            return
        allow_self = False
        wait = True
        if program.is_self:
            if not confirm(
                self, tr("Desinstalar o SafeOti"),
                tr("Quer mesmo desinstalar o SafeOti?"),
                tr("O desinstalador vai abrir; feche o SafeOti para que possa remover todos os ficheiros."),
                yes_text=tr("Desinstalar"), no_text=tr("Cancelar"),
            ):
                return
            allow_self, wait = True, False
        else:
            if program.requires_admin and not admin.is_admin():
                if confirm(
                    self, tr("Privilégios de administrador"),
                    tr("'{name}' foi instalado para todos os utilizadores e só pode ser desinstalado como "
                       "administrador.", name=program.name),
                    yes_text=tr("Reiniciar como administrador"), no_text=tr("Cancelar"), danger=False,
                ):
                    request_admin_relaunch(self)
                return
            if program.is_store:
                detail = tr("A aplicação e os seus dados locais serão removidos deste utilizador.")
            else:
                detail = tr("Vai abrir o desinstalador do programa, onde poderá confirmar ou cancelar.")
            if not confirm(self, tr("Desinstalar"), tr("Desinstalar '{name}'?", name=program.name), detail,
                           yes_text=tr("Desinstalar"), no_text=tr("Cancelar")):
                return
        self.status.set_message(tr("A desinstalar '{name}'…", name=program.name))
        self._uninstall_worker = start_task(
            _run_uninstall, program, allow_self, wait,
            with_progress=True,
            on_success=self._on_uninstalled,
            on_error=self._on_uninstall_error,
            on_progress=lambda _pct, msg: self.status.set_message(msg),
        )
        self._update_buttons()

    def _on_uninstall_error(self, message: str) -> None:
        self.status.set_message(message, "error")
        show_error(self, tr("Desinstalar"), message)
        self._update_buttons()

    def _on_uninstalled(self, outcome: Tuple[str, object]) -> None:
        kind, payload = outcome
        if kind == "admin":
            self._update_buttons()
            self.status.set_message(str(payload), "error")
            if confirm(self, tr("Privilégios de administrador"), str(payload),
                       yes_text=tr("Reiniciar como administrador"), no_text=tr("Cancelar"), danger=False):
                request_admin_relaunch(self)
            return
        if kind != "ok":
            self._on_uninstall_error(str(payload))
            return
        result: UninstallResult = payload  # type: ignore[assignment]
        if result.launched_only:
            message = tr("Desinstalador de '{name}' aberto.", name=result.name)
            self.status.set_message(message, "success")
            self.status_message.emit(message)
            show_info(self, tr("Desinstalar"), message, tr("Feche o SafeOti para concluir a desinstalação."))
        elif result.stopped_waiting:
            pass
        elif result.cancelled:
            self.status.set_message(tr("Desinstalação de '{name}' cancelada.", name=result.name), "warning")
        elif result.reboot_required:
            message = tr("'{name}' foi desinstalado. Reinicie o PC para concluir.", name=result.name)
            self.status.set_message(message, "success")
            self.status_message.emit(message)
        elif result.ok:
            message = tr("Desinstalador de '{name}' terminado.", name=result.name)
            self.status.set_message(message, "success")
            self.status_message.emit(message)
        else:
            self.status.set_message(tr("O desinstalador de '{name}' terminou com o código {code}.",
                                       name=result.name, code=result.returncode), "warning")
        self._update_buttons()
        self.refresh()

    def shutdown(self) -> None:
        # Deixa de esperar pelo desinstalador (que continua aberto) e pela listagem.
        for worker in (self._uninstall_worker, self._worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
