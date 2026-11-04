"""Página "Limpeza": análise e remoção de ficheiros temporários."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import admin, cleaner
from core.cleaner import CategoryScan, CleanSummary
from core.i18n import tr
from core.utils import format_bytes
from ui.common import AdminNotice, PageBase, StatusLine, button, confirm, page_header, show_error
from ui.common import PAGE_MARGINS, PAGE_SPACING, style_table
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.cleaner")

COL_NAME, COL_DESC, COL_FILES, COL_SIZE, COL_STATUS = range(5)


class CleanerPage(PageBase):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._worker: Optional[TaskWorker] = None
        self._scan: Dict[str, CategoryScan] = {}
        self._items: Dict[str, QTreeWidgetItem] = {}
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(
            tr("Limpeza"),
            tr("Analise primeiro para ver quanto espaço pode libertar. Só são apagados ficheiros dentro das pastas "
               "de cada categoria; ficheiros em uso são ignorados."),
        ))
        layout.addWidget(AdminNotice(
            tr("Sem privilégios de administrador, as categorias 'Temp do Windows' e 'Cache do Windows Update' "
               "só podem ser limpas parcialmente.")
        ))

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels([tr("Categoria"), tr("Descrição"), tr("Ficheiros"), tr("Tamanho"), tr("Estado")])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.NoSelection)
        self.tree.setFocusPolicy(Qt.NoFocus)
        style_table(self.tree)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_NAME, 260)
        header.setSectionResizeMode(COL_DESC, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_FILES, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        is_admin = admin.is_admin()
        for category in cleaner.CATEGORIES:
            description = category.description
            if category.requires_admin and not is_admin:
                description += tr(" (requer administrador)")
            item = QTreeWidgetItem(["  " + category.name, description, "—", "—", tr("Por analisar")])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(COL_NAME, Qt.Checked if category.default_selected else Qt.Unchecked)
            item.setToolTip(COL_DESC, description)
            item.setTextAlignment(COL_FILES, Qt.AlignRight | Qt.AlignVCenter)
            item.setTextAlignment(COL_SIZE, Qt.AlignRight | Qt.AlignVCenter)
            item.setData(COL_NAME, Qt.UserRole, category.id)
            self.tree.addTopLevelItem(item)
            self._items[category.id] = item
        for col in (COL_FILES, COL_SIZE):
            self.tree.headerItem().setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.tree, 1)

        selection = QHBoxLayout()
        self.btn_all = button(tr("Selecionar tudo"))
        self.btn_none = button(tr("Limpar seleção"))
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        selection.addWidget(self.btn_all)
        selection.addWidget(self.btn_none)
        selection.addStretch(1)
        self.total_label = QLabel("")
        self.total_label.setObjectName("TotalLabel")
        selection.addWidget(self.total_label)
        layout.addLayout(selection)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setObjectName("TaskProgress")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = StatusLine()
        self.status.set_message(tr("Escolha as categorias e carregue em 'Analisar'."))
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.btn_cancel = button(tr("Cancelar"))
        self.btn_cancel.setEnabled(False)
        self.btn_scan = button(tr("Analisar"), "primary")
        self.btn_clean = button(tr("Limpar"), "danger")
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_scan.clicked.connect(self._start_scan)
        self.btn_clean.clicked.connect(self._start_clean)
        actions.addWidget(self.btn_cancel)
        actions.addWidget(self.btn_scan)
        actions.addWidget(self.btn_clean)
        layout.addLayout(actions)

    # -------------------------------------------------------------- helpers --
    def _set_all(self, checked: bool) -> None:
        for item in self._items.values():
            item.setCheckState(COL_NAME, Qt.Checked if checked else Qt.Unchecked)

    def _selected(self) -> List[str]:
        return [cid for cid, item in self._items.items() if item.checkState(COL_NAME) == Qt.Checked]

    def _set_busy(self, busy: bool) -> None:
        self.btn_scan.setEnabled(not busy)
        self.btn_clean.setEnabled(not busy)
        self.btn_all.setEnabled(not busy)
        self.btn_none.setEnabled(not busy)
        self.tree.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        self.progress.setVisible(busy)

    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _on_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(max(0, min(100, percent)))
        self.status.set_message(message)

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status.set_message(tr("A cancelar…"), "warning")

    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        self.status.set_message(message, "error")
        show_error(self, tr("Limpeza"), message)

    # ------------------------------------------------------------- análise --
    def _start_scan(self) -> None:
        selected = self._selected()
        if not selected:
            self.status.set_message(tr("Selecione pelo menos uma categoria."), "warning")
            return
        for cid in selected:
            item = self._items[cid]
            item.setText(COL_FILES, "…")
            item.setText(COL_SIZE, "…")
            item.setText(COL_STATUS, tr("A analisar…"))
        self.progress.setValue(0)
        self._set_busy(True)
        self.status_message.emit(tr("A analisar ficheiros temporários…"))
        self._worker = start_task(
            cleaner.scan, selected, with_progress=True,
            on_success=self._on_scan_done, on_error=self._on_error, on_progress=self._on_progress,
            on_cancel=self._on_scan_cancelled,
        )

    def _on_scan_cancelled(self) -> None:
        self._set_busy(False)
        for item in self._items.values():
            if item.text(COL_STATUS) == tr("A analisar…"):
                item.setText(COL_FILES, "—")
                item.setText(COL_SIZE, "—")
                item.setText(COL_STATUS, tr("Cancelado"))
        self.status.set_message(tr("Análise cancelada."), "warning")

    def _on_scan_done(self, results: Dict[str, CategoryScan]) -> None:
        self._set_busy(False)
        self._scan.update(results)
        total_bytes = total_files = 0
        for cid, scan in results.items():
            item = self._items[cid]
            item.setText(COL_FILES, f"{scan.files:,}".replace(",", " "))
            item.setText(COL_SIZE, format_bytes(scan.bytes))
            if scan.error:
                item.setText(COL_STATUS, tr("Aviso"))
                item.setToolTip(COL_STATUS, scan.error)
            elif scan.files == 0:
                item.setText(COL_STATUS, tr("Nada a limpar"))
            else:
                if scan.inaccessible:
                    status = tr("Analisado ({count} pastas inacessíveis)", count=scan.inaccessible)
                else:
                    status = tr("Analisado")
                item.setText(COL_STATUS, status)
            total_bytes += scan.bytes
            total_files += scan.files
        files_text = f"{total_files:,}".replace(",", " ")
        if total_files == 1:
            total_text = tr("Total encontrado: {size} em {count} ficheiro",
                            size=format_bytes(total_bytes), count=files_text)
        else:
            total_text = tr("Total encontrado: {size} em {count} ficheiros",
                            size=format_bytes(total_bytes), count=files_text)
        self.total_label.setText(total_text)
        self.status.set_message(
            tr("Análise concluída: {size} podem ser libertados. Carregue em 'Limpar' para apagar.",
               size=format_bytes(total_bytes)),
            "success",
        )
        self.status_message.emit(tr("Análise concluída."))

    # ------------------------------------------------------------- limpeza --
    def _start_clean(self) -> None:
        selected = self._selected()
        if not selected:
            self.status.set_message(tr("Selecione pelo menos uma categoria."), "warning")
            return
        lines = []
        for cid in selected:
            category = cleaner.CATEGORY_BY_ID[cid]
            scan = self._scan.get(cid)
            size = (tr(" — {size} ({count} ficheiros)", size=format_bytes(scan.bytes), count=scan.files)
                    if scan else tr(" — não analisado"))
            lines.append(f"• {category.name}{size}")
        detail = "\n".join(lines)
        extra = []
        if "recycle_bin" in selected:
            extra.append(tr("A Reciclagem vai ser esvaziada: os ficheiros lá contidos deixam de poder ser recuperados."))
        if any(c in selected for c in ("chrome", "edge", "firefox")):
            extra.append(tr("Feche os browsers antes de limpar para libertar mais espaço (ficheiros em uso são ignorados)."))
        if extra:
            detail += "\n\n" + "\n".join(extra)
        if not confirm(self, tr("Confirmar limpeza"), tr("Vão ser apagados os ficheiros das seguintes categorias:"),
                       detail, yes_text=tr("Limpar"), no_text=tr("Cancelar")):
            return
        for cid in selected:
            self._items[cid].setText(COL_STATUS, tr("A limpar…"))
        self.progress.setValue(0)
        self._set_busy(True)
        self.status_message.emit(tr("A limpar ficheiros temporários…"))
        expected = {cid: s.files for cid, s in self._scan.items()}
        self._worker = start_task(
            cleaner.clean, selected, with_progress=True, expected_files=expected,
            on_success=self._on_clean_done, on_error=self._on_error, on_progress=self._on_progress,
        )

    def _on_clean_done(self, summary: CleanSummary) -> None:
        self._set_busy(False)
        for result in summary.results:
            item = self._items[result.category_id]
            text = tr("Libertado {size}", size=format_bytes(result.freed_bytes))
            if result.skipped_files:
                text += tr(" ({count} em uso/sem permissão)", count=result.skipped_files)
            if result.error:
                item.setToolTip(COL_STATUS, result.error)
            item.setText(COL_STATUS, text)
            self._scan.pop(result.category_id, None)
            item.setText(COL_FILES, "—")
            item.setText(COL_SIZE, "—")
        for item in self._items.values():
            if item.text(COL_STATUS) == tr("A limpar…"):
                item.setText(COL_STATUS, tr("Cancelado"))
        freed = format_bytes(summary.freed_bytes)
        if summary.cancelled:
            message = tr("Limpeza cancelada. Espaço libertado até ao cancelamento: {freed}.", freed=freed)
            kind = "warning"
        else:
            message = tr("Limpeza concluída. Espaço libertado: {freed} ({count} ficheiros apagados).",
                         freed=freed, count=summary.deleted_files)
            if summary.skipped_files:
                message += tr(" {count} ficheiros foram ignorados por estarem em uso ou sem permissão.",
                              count=summary.skipped_files)
            kind = "success"
        self.total_label.setText(tr("Espaço libertado: {freed}", freed=freed))
        self.status.set_message(message, kind)
        self.status_message.emit(message)

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
