"""Desinstalador do SafeOti, com o mesmo visual do instalador.

Corre dentro do próprio SafeOti.exe (SafeOti.exe --desinstalar), que é o que o
Windows chama a partir de "Aplicações instaladas". Assim não é preciso um
segundo executável e o PySide6 já embutido é reaproveitado.
"""

from __future__ import annotations

import os
import sys
import traceback

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from core import __version__
from installer import ops
from installer.theme import (
    ProgressLine, Shell, SuccessMark, Toggle, label, option_row, page_layout, prepare_app, spacer,
)
from ui.icons import app_icon

STEPS = ["Confirmar", "Remoção", "Concluído"]


def install_folder() -> str:
    info = ops.read_installed()
    if info and info.location and os.path.isfile(os.path.join(info.location, ops.EXE_NAME)):
        return info.location
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return info.location if info else ""


class UninstallWorker(QObject):
    progress = Signal(float, str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, remove_data: bool) -> None:
        super().__init__()
        self.remove_data = remove_data

    def run(self) -> None:
        try:
            ops.uninstall("", self.remove_data, lambda f, m: self.progress.emit(f, m))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(f"Ocorreu um erro: {exc}")
        else:
            self.finished.emit()


class Uninstaller(Shell):
    def __init__(self) -> None:
        super().__init__(f"Desinstalar · versão {__version__}", STEPS)
        self.setWindowTitle("Desinstalar o SafeOti")
        self.folder = install_folder()
        self.done = False

        self.add_page(self._confirm_page())
        self.add_page(self._progress_page())
        self.add_page(self._done_page())
        self.btn_back.setText("Cancelar")
        self.btn_back.clicked.connect(self.close)
        self.btn_next.clicked.connect(self._next)
        self._go(0)

    def _confirm_page(self) -> QWidget:
        page = QWidget()
        lay = page_layout(page)
        lay.addWidget(spacer(14))
        lay.addWidget(label("Desinstalar o SafeOti?", "Title"))
        lay.addWidget(spacer(8))
        lay.addWidget(label("Vamos remover a aplicação, os atalhos e o arranque automático com o Windows.",
                            "Lead", wrap=True))
        lay.addWidget(spacer(10))
        if self.folder:
            lay.addWidget(label(f"Pasta: {self.folder}", "Small", wrap=True))
        lay.addWidget(spacer(26))
        self.t_data = Toggle(False)
        lay.addWidget(option_row("definicoes", "Apagar também as definições e registos",
                                 f"{ops.data_dir()}", self.t_data))
        lay.addWidget(spacer(12))
        lay.addWidget(label("Se o Modo Gaming estiver ativo, desative-o no SafeOti antes de desinstalar.",
                            "Small", wrap=True))
        lay.addStretch(1)
        return page

    def _progress_page(self) -> QWidget:
        page = QWidget()
        lay = page_layout(page)
        lay.addWidget(spacer(14))
        self.p_title = label("A remover…", "Title")
        lay.addWidget(self.p_title)
        lay.addStretch(1)
        self.percent = label("0%", "Big")
        lay.addWidget(self.percent)
        lay.addWidget(spacer(10))
        self.bar = ProgressLine()
        lay.addWidget(self.bar)
        lay.addWidget(spacer(10))
        self.status = label("", "Muted")
        lay.addWidget(self.status)
        lay.addWidget(spacer(14))
        self.error = label("", "Error", wrap=True)
        self.error.hide()
        lay.addWidget(self.error)
        lay.addStretch(2)
        return page

    def _done_page(self) -> QWidget:
        page = QWidget()
        lay = page_layout(page)
        lay.addStretch(1)
        self.mark = SuccessMark(76)
        lay.addWidget(self.mark, 0, Qt.AlignLeft)
        lay.addWidget(spacer(22))
        lay.addWidget(label("O SafeOti foi removido", "Title"))
        lay.addWidget(spacer(6))
        lay.addWidget(label("Obrigado por ter experimentado. A pasta da aplicação é apagada "
                            "assim que fechar esta janela.", "Lead", wrap=True))
        lay.addStretch(2)
        return page

    def _go(self, index: int) -> None:
        self.index = index
        self.set_step(index)
        self.show_page(index)
        self.btn_back.setVisible(index == 0)
        self.btn_next.setEnabled(index != 1)
        self.btn_next.setText({0: "Desinstalar", 1: "A remover…", 2: "Fechar"}[index])
        if index == 2:
            QTimer.singleShot(150, self.mark.play)

    def _next(self) -> None:
        if self.index == 0:
            self._start()
        elif self.index == 2:
            self.close()

    def _start(self) -> None:
        others = ops.running_safeoti_pids(exclude=(os.getpid(), os.getppid()))
        self._go(1)
        self.p_title.setText("A remover…")
        self.error.hide()
        self.bar.reset()
        self.percent.setText("0%")
        if others:
            self._fail("O SafeOti ainda está aberto noutra janela (ou na área de notificação). "
                       "Feche-o e tente de novo.")
            return
        self.bar.set_running(True)
        self.can_close = False
        self.btn_close.setEnabled(False)
        self._thread = QThread(self)
        self._worker = UninstallWorker(self.t_data.isChecked())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.failed.connect(self._fail)
        self._worker.finished.connect(self._finished)
        for sig in (self._worker.failed, self._worker.finished):
            sig.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self.bar.set_target(fraction)
        self.percent.setText(f"{int(fraction * 100)}%")
        self.status.setText(message)

    def _fail(self, message: str) -> None:
        self.can_close = True
        self.btn_close.setEnabled(True)
        self.bar.set_running(False)
        self.p_title.setText("Não foi possível desinstalar")
        self.error.setText(message)
        self.error.show()
        self.btn_next.setText("Tentar de novo")
        self.btn_next.setEnabled(True)
        self.index = 0  # o botão principal volta a iniciar a remoção

    def _finished(self) -> None:
        self.done = True
        self.bar.set_running(False)
        self.can_close = True
        self.btn_close.setEnabled(True)
        QTimer.singleShot(400, lambda: self._go(2))


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    prepare_app(app)
    app.setWindowIcon(app_icon())
    window = Uninstaller()
    window.appear()
    code = app.exec()
    if window.done and window.folder and getattr(sys, "frozen", False):
        ops.schedule_folder_removal(window.folder)
    return code
