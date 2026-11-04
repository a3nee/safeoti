"""Página "Espaço em disco": que pastas e ficheiros ocupam mais espaço."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import List, Optional

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QProgressBar,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import diskspace
from core.diskspace import DirNode, DiskSpaceError, FileEntry, ScanResult
from core.i18n import format_decimal, tr
from core.utils import IS_WINDOWS, format_bytes
from ui.common import (
    PAGE_MARGINS,
    PAGE_SPACING,
    PageBase,
    StatusLine,
    button,
    confirm,
    page_header,
    show_error,
    show_warning,
    style_table,
)
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.diskspace")

COL_NAME, COL_SIZE, COL_SHARE, COL_FILES = range(4)
FCOL_NAME, FCOL_FOLDER, FCOL_SIZE = range(3)

ROLE_NODE = Qt.UserRole          # DirNode (coluna Nome)
ROLE_KIND = Qt.UserRole + 1      # "dir" | "files" | "file"
ROLE_SORT = Qt.UserRole + 2      # chave de ordenação por coluna
ROLE_LOADED = Qt.UserRole + 3    # filhos já criados (carregamento preguiçoso)
ROLE_SHARE = Qt.UserRole + 4     # fração 0..1 (coluna % do pai)
ROLE_PATH = Qt.UserRole + 5      # caminho (lista de ficheiros)


def _count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _share_text(fraction: float) -> str:
    return f"{format_decimal(fraction * 100, 1)} %"


class _SortItem(QTreeWidgetItem):
    """Ordena pela chave numérica guardada em ROLE_SORT (tamanhos, contagens)."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:  # type: ignore[override]
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 0
        a, b = self.data(col, ROLE_SORT), other.data(col, ROLE_SORT)
        if a is None or b is None:
            return self.text(col).lower() < other.text(col).lower()
        return a < b


class _ShareDelegate(QStyledItemDelegate):
    """Barra de proporção discreta com as cores da paleta (tema claro e escuro)."""

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: D401
        fraction = index.data(ROLE_SHARE)
        if fraction is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        selected = bool(opt.state & QStyle.State_Selected)
        ink = QColor(opt.palette.color(QPalette.HighlightedText if selected else QPalette.Text))
        rect = opt.rect.adjusted(8, 0, -8, 0)
        text_w = opt.fontMetrics.horizontalAdvance("100,0 %") + 10
        bar_w = max(16, rect.width() - text_w)
        bar = QRect(rect.left(), rect.center().y() - 3, bar_w, 6)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        track = QColor(ink)
        track.setAlpha(36)
        painter.setBrush(track)
        painter.drawRoundedRect(bar, 3, 3)
        filled = int(round(bar.width() * max(0.0, min(1.0, float(fraction)))))
        if filled > 0:
            fill = QColor(ink)
            fill.setAlpha(190)
            painter.setBrush(fill)
            painter.drawRoundedRect(QRect(bar.left(), bar.top(), max(filled, 3), bar.height()), 3, 3)
        painter.setPen(ink)
        painter.drawText(QRect(bar.right() + 6, rect.top(), rect.right() - bar.right() - 6, rect.height()),
                         int(Qt.AlignRight | Qt.AlignVCenter), text)
        painter.restore()


class DiskSpacePage(PageBase):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._worker: Optional[TaskWorker] = None
        self._recycle_worker: Optional[TaskWorker] = None
        self._result: Optional[ScanResult] = None
        self._loaded = False
        self._build()

    # ------------------------------------------------------------- UI --
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(
            tr("Espaço em disco"),
            tr("Descubra que pastas e ficheiros ocupam mais espaço. Nada é apagado sem confirmação, "
               "e o que remover vai para a Reciclagem."),
        ))

        top = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setMinimumWidth(320)
        self.combo.setToolTip(tr("Unidade ou pasta a analisar"))
        top.addWidget(self.combo, 1)
        self.btn_folder = button(tr("Escolher pasta…"), icon_name="pasta")
        self.btn_folder.clicked.connect(self._choose_folder)
        top.addWidget(self.btn_folder)
        self.btn_scan = button(tr("Analisar"), "primary", icon_name="pesquisa")
        self.btn_scan.clicked.connect(self._start_scan)
        top.addWidget(self.btn_scan)
        self.btn_cancel = button(tr("Cancelar"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        top.addWidget(self.btn_cancel)
        layout.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setObjectName("TaskProgress")
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        tabs = QHBoxLayout()
        self.btn_tab_dirs = button(tr("Pastas"))
        self.btn_tab_files = button(tr("Maiores ficheiros"))
        self.btn_tab_dirs.clicked.connect(lambda: self._show_tab(0))
        self.btn_tab_files.clicked.connect(lambda: self._show_tab(1))
        tabs.addWidget(self.btn_tab_dirs)
        tabs.addWidget(self.btn_tab_files)
        tabs.addStretch(1)
        layout.addLayout(tabs)

        self.stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([tr("Nome"), tr("Tamanho"), tr("% do pai"), tr("Ficheiros")])
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setItemDelegateForColumn(COL_SHARE, _ShareDelegate(self.tree))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.itemExpanded.connect(self._populate)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_SIZE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_SHARE, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_FILES, QHeaderView.ResizeToContents)
        self.tree.setColumnWidth(COL_SHARE, 200)
        for col in (COL_SIZE, COL_FILES):
            self.tree.headerItem().setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(COL_SIZE, Qt.DescendingOrder)
        self.stack.addWidget(self.tree)

        self.files = QTreeWidget()
        self.files.setColumnCount(3)
        self.files.setHeaderLabels([tr("Nome"), tr("Pasta"), tr("Tamanho")])
        self.files.setRootIsDecorated(False)
        self.files.setUniformRowHeights(True)
        self.files.setSelectionMode(QAbstractItemView.SingleSelection)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._files_menu)
        self.files.itemSelectionChanged.connect(self._update_buttons)
        self.files.itemDoubleClicked.connect(lambda *_: self._open_selected())
        fheader = self.files.header()
        fheader.setStretchLastSection(False)
        fheader.setSectionResizeMode(FCOL_NAME, QHeaderView.Interactive)
        fheader.setSectionResizeMode(FCOL_FOLDER, QHeaderView.Stretch)
        fheader.setSectionResizeMode(FCOL_SIZE, QHeaderView.ResizeToContents)
        self.files.setColumnWidth(FCOL_NAME, 260)
        self.files.headerItem().setTextAlignment(FCOL_SIZE, Qt.AlignRight | Qt.AlignVCenter)
        self.files.setSortingEnabled(True)
        self.files.sortByColumn(FCOL_SIZE, Qt.DescendingOrder)
        self.stack.addWidget(self.files)
        for view in (self.tree, self.files):
            view.setTextElideMode(Qt.ElideMiddle)
            style_table(view)
        layout.addWidget(self.stack, 1)

        actions = QHBoxLayout()
        self.btn_open = button(tr("Abrir no Explorador"), icon_name="abrir")
        self.btn_open.clicked.connect(self._open_selected)
        actions.addWidget(self.btn_open)
        self.btn_recycle = button(tr("Enviar para a Reciclagem"), "danger", icon_name="lixo")
        self.btn_recycle.clicked.connect(self._recycle_selected)
        actions.addWidget(self.btn_recycle)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status = StatusLine()
        self.status.set_message(tr("Escolha uma unidade ou pasta e carregue em 'Analisar'."))
        layout.addWidget(self.status)

        self._show_tab(0)
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self._load_drives()

    # ------------------------------------------------------- unidades --
    def _load_drives(self, keep: str = "") -> None:
        current = keep or (self.combo.currentData() or "")
        custom = [(self.combo.itemText(i), self.combo.itemData(i)) for i in range(self.combo.count())
                  if self.combo.itemData(i, Qt.UserRole + 1)]
        self.combo.clear()
        for drive in diskspace.list_drives():
            name = drive.mountpoint.rstrip("\\/") or drive.mountpoint
            if drive.label:
                name = f"{name} ({drive.label})"
            text = tr("{drive} — {used} de {total} usados ({free} livres)", drive=name,
                      used=format_bytes(drive.used), total=format_bytes(drive.total),
                      free=format_bytes(drive.free))
            self.combo.addItem(text, drive.mountpoint)
        for text, path in custom:
            self._add_custom(path, text)
        index = self.combo.findData(current)
        if index >= 0:
            self.combo.setCurrentIndex(index)

    def _add_custom(self, path: str, text: str = "") -> int:
        index = self.combo.findData(path)
        if index < 0:
            self.combo.addItem(text or tr("Pasta: {path}", path=path), path)
            index = self.combo.count() - 1
            self.combo.setItemData(index, True, Qt.UserRole + 1)
        return index

    def _choose_folder(self) -> None:
        start = self.combo.currentData() or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, tr("Escolher pasta para analisar"), start)
        if not path:
            return
        path = os.path.normpath(path)
        self.combo.setCurrentIndex(self._add_custom(path))

    # -------------------------------------------------------- análise --
    def is_busy(self) -> bool:
        return any(w is not None and w.isRunning() for w in (self._worker, self._recycle_worker))

    def _set_busy(self, busy: bool, scanning: bool = False) -> None:
        for widget in (self.combo, self.btn_folder, self.btn_scan):
            widget.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy and scanning)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        self._update_buttons()

    def _start_scan(self) -> None:
        root = self.combo.currentData()
        if not root or self.is_busy():
            return
        self.tree.clear()
        self.files.clear()
        self._result = None
        self._set_busy(True, scanning=True)
        self.status.set_message(tr("A analisar {path}…", path=root))
        self._worker = start_task(
            _scan_job, root, with_progress=True,
            on_success=self._on_scan_done, on_error=self._on_error,
            on_progress=self._on_progress, on_cancel=self._on_cancelled,
        )

    def _cancel(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.status.set_message(tr("A cancelar…"), "warning")

    def _on_progress(self, percent: int, path: str) -> None:
        if percent > 0:
            if self.progress.maximum() == 0:
                self.progress.setRange(0, 100)
            self.progress.setValue(min(100, percent))
        self.status.set_message(tr("A analisar: {path}", path=path))

    def _on_cancelled(self) -> None:
        self._set_busy(False)
        self.status.set_message(tr("Análise cancelada."), "warning")

    def _on_error(self, message: str) -> None:
        self._set_busy(False)
        self.status.set_message(message, "error")
        self.status_message.emit(message)

    def _on_scan_done(self, outcome) -> None:
        self._set_busy(False)
        if isinstance(outcome, str):  # erro com mensagem pronta
            self._on_error(outcome)
            return
        result: ScanResult = outcome
        self._result = result
        self._fill_views()
        root = result.root
        parts = [tr("{size} em {files} ficheiros e {folders} pastas ({seconds} s).",
                    size=format_bytes(root.size), files=_count(root.file_count),
                    folders=_count(result.dir_count), seconds=format_decimal(result.elapsed, 1))]
        if result.inaccessible:
            parts.append(tr("{count} itens sem acesso foram ignorados.", count=_count(result.inaccessible)))
        if result.links_skipped:
            parts.append(tr("{count} atalhos (symlinks/junctions) não foram seguidos.",
                            count=_count(result.links_skipped)))
        message = " ".join(parts)
        self.status.set_message(message, "success")
        self.status_message.emit(tr("Análise de espaço concluída: {size}", size=format_bytes(root.size)))

    # --------------------------------------------------------- árvore --
    def _fill_views(self) -> None:
        result = self._result
        if result is None:
            return
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        root_item = self._make_dir_item(result.root, result.root.size)
        root_item.setText(COL_NAME, result.root.path)
        self.tree.addTopLevelItem(root_item)
        root_item.setExpanded(True)  # cria o primeiro nível
        self.tree.setUpdatesEnabled(True)
        self._fill_files()

    def _fill_files(self) -> None:
        self.files.setSortingEnabled(False)
        self.files.clear()
        items = [self._make_file_item(entry) for entry in (self._result.largest if self._result else [])]
        self.files.addTopLevelItems(items)
        self.files.setSortingEnabled(True)

    def _make_file_item(self, entry: FileEntry) -> QTreeWidgetItem:
        item = _SortItem()
        item.setText(FCOL_NAME, entry.name)
        item.setToolTip(FCOL_NAME, entry.path)
        item.setText(FCOL_FOLDER, entry.folder)
        item.setToolTip(FCOL_FOLDER, entry.folder)
        item.setText(FCOL_SIZE, format_bytes(entry.size))
        item.setTextAlignment(FCOL_SIZE, Qt.AlignRight | Qt.AlignVCenter)
        item.setData(FCOL_SIZE, ROLE_SORT, entry.size)
        item.setData(FCOL_NAME, ROLE_KIND, "file")
        item.setData(FCOL_NAME, ROLE_PATH, entry.path)
        item.setData(FCOL_NAME, ROLE_NODE, entry)
        return item

    def _make_dir_item(self, node: DirNode, parent_size: int) -> QTreeWidgetItem:
        item = _SortItem()
        item.setData(COL_NAME, ROLE_NODE, node)
        item.setData(COL_NAME, ROLE_KIND, "dir")
        item.setData(COL_NAME, ROLE_LOADED, False)
        item.setText(COL_NAME, node.name)
        if node.children or node.own_files:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        else:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
        self._update_dir_item(item, parent_size)
        return item

    def _update_dir_item(self, item: QTreeWidgetItem, parent_size: int) -> None:
        kind = item.data(COL_NAME, ROLE_KIND)
        node: DirNode = item.data(COL_NAME, ROLE_NODE)
        if kind == "files":
            size, files = node.own_size, node.own_files
        else:
            size, files = node.size, node.file_count
            tip = node.path
            if node.inaccessible:
                tip += "\n" + tr("Sem acesso: o conteúdo desta pasta não foi contado.")
            item.setToolTip(COL_NAME, tip)
        fraction = (size / parent_size) if parent_size else 0.0
        item.setText(COL_SIZE, format_bytes(size))
        item.setData(COL_SIZE, ROLE_SORT, size)
        item.setTextAlignment(COL_SIZE, Qt.AlignRight | Qt.AlignVCenter)
        item.setText(COL_SHARE, _share_text(fraction))
        item.setData(COL_SHARE, ROLE_SHARE, fraction)
        item.setData(COL_SHARE, ROLE_SORT, size)
        item.setText(COL_FILES, _count(files))
        item.setData(COL_FILES, ROLE_SORT, files)
        item.setTextAlignment(COL_FILES, Qt.AlignRight | Qt.AlignVCenter)

    def _populate(self, item: QTreeWidgetItem) -> None:
        if item.data(COL_NAME, ROLE_KIND) != "dir" or item.data(COL_NAME, ROLE_LOADED):
            return
        item.setData(COL_NAME, ROLE_LOADED, True)
        node: DirNode = item.data(COL_NAME, ROLE_NODE)
        children: List[QTreeWidgetItem] = [self._make_dir_item(child, node.size) for child in node.children]
        if node.own_files:
            files_item = _SortItem()
            files_item.setData(COL_NAME, ROLE_NODE, node)
            files_item.setData(COL_NAME, ROLE_KIND, "files")
            files_item.setText(COL_NAME, tr("(ficheiros nesta pasta)"))
            files_item.setToolTip(COL_NAME, tr("Ficheiros diretamente dentro de {path}", path=node.path))
            self._update_dir_item(files_item, node.size)
            children.append(files_item)
        self.tree.setUpdatesEnabled(False)
        item.addChildren(children)
        self.tree.setUpdatesEnabled(True)

    def _item_for_node(self, node: DirNode) -> Optional[QTreeWidgetItem]:
        chain = []
        current: Optional[DirNode] = node
        while current is not None:
            chain.append(current)
            current = current.parent
        chain.reverse()
        item = self.tree.topLevelItem(0)
        if item is None or item.data(COL_NAME, ROLE_NODE) is not chain[0]:
            return None
        for wanted in chain[1:]:
            if not item.data(COL_NAME, ROLE_LOADED):
                return None
            item = next((item.child(i) for i in range(item.childCount())
                         if item.child(i).data(COL_NAME, ROLE_KIND) == "dir"
                         and item.child(i).data(COL_NAME, ROLE_NODE) is wanted), None)
            if item is None:
                return None
        return item

    def _refresh_upwards(self, item: Optional[QTreeWidgetItem]) -> None:
        """Atualiza os números de ``item``, dos seus filhos e de todos os antecessores."""
        while item is not None:
            node: DirNode = item.data(COL_NAME, ROLE_NODE)
            parent = item.parent()
            parent_size = parent.data(COL_NAME, ROLE_NODE).size if parent else node.size
            self._update_dir_item(item, parent_size)
            for i in range(item.childCount()):
                self._update_dir_item(item.child(i), node.size)
            item = parent

    # -------------------------------------------------------- seleção --
    def _show_tab(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate((self.btn_tab_dirs, self.btn_tab_files)):
            btn.setProperty("variant", "primary" if i == index else "")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._update_buttons()

    def _selected(self):
        """Devolve (item, tipo, caminho) do item selecionado na vista visível."""
        view = self.tree if self.stack.currentIndex() == 0 else self.files
        items = view.selectedItems()
        if not items:
            return None, "", ""
        item = items[0]
        kind = item.data(0, ROLE_KIND)
        if kind == "file":
            return item, kind, item.data(0, ROLE_PATH)
        node: DirNode = item.data(COL_NAME, ROLE_NODE)
        return item, kind, node.path

    def _update_buttons(self) -> None:
        item, kind, _path = self._selected()
        busy = self.is_busy()
        self.btn_open.setEnabled(item is not None)
        can_recycle = item is not None and not busy and (
            kind == "file" or (kind == "dir" and item.parent() is not None)
        )
        self.btn_recycle.setEnabled(can_recycle)

    def _tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is not None:
            self.tree.setCurrentItem(item)
            self._context_menu(self.tree, pos)

    def _files_menu(self, pos) -> None:
        item = self.files.itemAt(pos)
        if item is not None:
            self.files.setCurrentItem(item)
            self._context_menu(self.files, pos)

    def _context_menu(self, view: QTreeWidget, pos) -> None:
        self._update_buttons()
        menu = QMenu(self)
        act_open = menu.addAction(tr("Abrir no Explorador"))
        act_open.setEnabled(self.btn_open.isEnabled())
        act_recycle = menu.addAction(tr("Enviar para a Reciclagem"))
        act_recycle.setEnabled(self.btn_recycle.isEnabled())
        chosen = menu.exec(view.viewport().mapToGlobal(pos))
        if chosen is act_open:
            self._open_selected()
        elif chosen is act_recycle:
            self._recycle_selected()

    # ---------------------------------------------------------- ações --
    def _open_selected(self) -> None:
        item, kind, path = self._selected()
        if item is None or not path:
            return
        try:
            if not os.path.lexists(path):
                show_warning(self, tr("Abrir no Explorador"), tr("O item já não existe."), path)
                return
            if IS_WINDOWS:
                drive, rest = os.path.splitdrive(path)
                if kind == "files" or not rest.strip("\\/"):
                    subprocess.Popen(["explorer.exe", os.path.normpath(path)])  # noqa: S603,S607
                else:
                    subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])  # noqa: S603,S607
            else:
                folder = path if kind == "files" else os.path.dirname(path)
                subprocess.Popen(["xdg-open", folder])  # noqa: S603,S607
        except OSError as exc:
            show_error(self, tr("Abrir no Explorador"), tr("Não foi possível abrir o Explorador."), str(exc))

    def _recycle_selected(self) -> None:
        item, kind, path = self._selected()
        if item is None or kind not in ("dir", "file") or self.is_busy():
            return
        if kind == "dir" and item.parent() is None:
            return
        reason = diskspace.protected_reason(path)
        if reason:
            show_warning(self, tr("Item protegido"),
                         tr("O SafeOti não envia este item para a Reciclagem."), f"{path}\n\n{reason}")
            log.info("Reciclagem recusada para %s: %s", path, reason)
            return
        if kind == "file":
            entry: FileEntry = item.data(0, ROLE_NODE)
            size = entry.size
        else:
            node: DirNode = item.data(COL_NAME, ROLE_NODE)
            size = node.size
        if not confirm(
            self, tr("Enviar para a Reciclagem"),
            tr("Enviar \"{name}\" ({size}) para a Reciclagem?", name=os.path.basename(path) or path,
               size=format_bytes(size)),
            tr("Pode restaurá-lo a partir da Reciclagem. Se o Windows não o conseguir reciclar "
               "(por exemplo, por ser demasiado grande), pergunta antes de o apagar definitivamente.")
            + "\n\n" + path,
            yes_text=tr("Enviar para a Reciclagem"), no_text=tr("Cancelar"),
        ):
            return
        target = item.data(0, ROLE_NODE)
        self.status.set_message(tr("A enviar para a Reciclagem: {path}", path=path))
        self._set_busy(True)
        self._recycle_worker = start_task(
            _recycle_job, path,
            on_success=lambda outcome: self._on_recycled(outcome, kind, path, target),
            on_error=self._on_recycle_error,
            on_cancel=self._on_recycle_cancelled,
        )

    def _on_recycle_error(self, message: str) -> None:
        self._set_busy(False)
        self.status.set_message(message, "error")
        show_error(self, tr("Enviar para a Reciclagem"), tr("Não foi possível enviar o item para a Reciclagem."),
                   message)

    def _on_recycle_cancelled(self) -> None:
        self._set_busy(False)
        self.status.set_message(tr("Operação cancelada."), "warning")

    def _on_recycled(self, outcome, kind: str, path: str, target) -> None:
        if isinstance(outcome, str):
            self._on_recycle_error(outcome)
            return
        self._set_busy(False)
        result = self._result
        size = target.size
        if result is not None:
            if kind == "dir":
                node: DirNode = target
                parent_node = node.parent
                result.remove_dir(node)
                item = self._item_for_node(node)
                if item is not None and item.parent() is not None:
                    parent_item = item.parent()
                    parent_item.removeChild(item)
                    self._refresh_upwards(parent_item)
                elif parent_node is not None:
                    self._refresh_upwards(self._item_for_node(parent_node))
            else:
                result.remove_file(path, size)
                folder = result.find_dir(os.path.dirname(path))
                if folder is not None:
                    self._refresh_upwards(self._item_for_node(folder))
            self._fill_files()
        self.status.set_message(tr("Enviado para a Reciclagem: {path} ({size}).", path=path,
                                   size=format_bytes(size)), "success")
        self.status_message.emit(tr("Enviado para a Reciclagem: {path} ({size}).", path=path,
                                    size=format_bytes(size)))
        self._load_drives()
        self._update_buttons()

    # ------------------------------------------------------ encerrar --
    def shutdown(self) -> None:
        """Pede à análise em curso para parar (chamado pela janela principal ao fechar)."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()


# Funções corridas na thread de trabalho: os erros esperados voltam como texto
# (já traduzido) para não serem registados como erros inesperados.
def _scan_job(root: str, progress, should_cancel):
    try:
        return diskspace.scan(root, progress=progress, should_cancel=should_cancel)
    except DiskSpaceError as exc:
        return str(exc)


def _recycle_job(path: str):
    try:
        diskspace.recycle(path)
    except DiskSpaceError as exc:
        return str(exc)
    return None
