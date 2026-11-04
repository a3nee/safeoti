"""Instalador do SafeOti — ponto de entrada do SafeOti-Setup.exe.

O SafeOti.exe compilado vai embutido no instalador (pasta payload/).
Em desenvolvimento: python -m installer.setup_main  (usa dist\\SafeOti.exe).
"""

from __future__ import annotations

import os
import sys
import traceback

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from core import __version__  # noqa: E402
from core.utils import format_bytes  # noqa: E402
from installer import ops  # noqa: E402
from installer.theme import (  # noqa: E402
    ProgressLine, Shell, SuccessMark, Toggle, icon_label, label, option_row, page_layout, prepare_app, spacer,
)
from ui.icons import app_icon  # noqa: E402

STEPS = ["Boas-vindas", "Opções", "Instalação", "Concluído"]


def payload_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "payload", ops.EXE_NAME)  # type: ignore[attr-defined]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "dist", ops.EXE_NAME)


class InstallWorker(QObject):
    progress = Signal(float, str)
    failed = Signal(str)
    finished = Signal(str)

    def __init__(self, folder: str, desktop: bool, start_menu: bool) -> None:
        super().__init__()
        self.folder, self.desktop, self.start_menu = folder, desktop, start_menu

    def run(self) -> None:
        try:
            exe = ops.install(payload_path(), self.folder, __version__, self.desktop, self.start_menu,
                              lambda f, m: self.progress.emit(f, m))
        except ops.InstallError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(f"Ocorreu um erro inesperado: {exc}")
        else:
            self.finished.emit(exe)


class Installer(Shell):
    def __init__(self) -> None:
        super().__init__(f"Instalador · versão {__version__}", STEPS)
        self.setWindowTitle("Instalar o SafeOti")
        self.installed = ops.read_installed()
        self.folder = self.installed.location if self.installed and self.installed.location else ops.default_install_dir()
        self.exe_path = ""
        self._thread: QThread | None = None

        self.add_page(self._welcome_page())
        self.add_page(self._options_page())
        self.add_page(self._progress_page())
        self.add_page(self._done_page())

        self.btn_back.clicked.connect(self._back)
        self.btn_next.clicked.connect(self._next)
        self._go(0)

    # ------------------------------------------------------------ páginas
    def _welcome_page(self) -> QWidget:
        page = QWidget()
        lay = page_layout(page)
        lay.addWidget(spacer(14))
        if self.installed:
            title = "Atualizar o SafeOti"
            lead = (f"Já tem o SafeOti {self.installed.version or ''} instalado. "
                    f"Vamos atualizá-lo para a versão {__version__} — as suas definições mantêm-se.")
        else:
            title = "Bem-vindo ao SafeOti"
            lead = "Deixe o seu PC mais leve e rápido. A instalação demora menos de um minuto."
        lay.addWidget(label(title, "Title"))
        lay.addWidget(spacer(8))
        lay.addWidget(label(lead, "Lead", wrap=True))
        lay.addWidget(spacer(28))
        features = [
            ("limpeza", "Limpeza segura", "Temporários, cache dos navegadores e Reciclagem — com pré-visualização."),
            ("processos", "Monitorização em tempo real", "CPU, RAM, disco e rede, com gráficos e temperaturas."),
            ("gaming", "Modo Gaming", "Mais desempenho para jogar, e tudo volta ao normal no fim."),
        ]
        for icon, head, text in features:
            row = QHBoxLayout()
            row.setSpacing(14)
            badge = QFrame()
            badge.setObjectName("Field")
            badge.setFixedSize(40, 40)
            bl = QVBoxLayout(badge)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.addWidget(icon_label(icon, 20), 0, Qt.AlignCenter)
            row.addWidget(badge, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(label(head, "FeatTitle"))
            col.addWidget(label(text, "Muted", wrap=True))
            row.addLayout(col, 1)
            lay.addLayout(row)
            lay.addWidget(spacer(14))
        lay.addStretch(1)
        return page

    def _options_page(self) -> QWidget:
        page = QWidget()
        lay = page_layout(page)
        lay.addWidget(spacer(14))
        lay.addWidget(label("Onde e como instalar", "Title"))
        lay.addWidget(spacer(6))
        lay.addWidget(label("A pasta sugerida não precisa de permissões especiais.", "Lead", wrap=True))
        lay.addWidget(spacer(22))

        field = QFrame()
        field.setObjectName("Field")
        fl = QHBoxLayout(field)
        fl.setContentsMargins(12, 6, 6, 6)
        fl.setSpacing(10)
        fl.addWidget(icon_label("pasta", 18))
        self.folder_label = QLabel()
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fl.addWidget(self.folder_label, 1)
        change = QPushButton("Alterar…")
        change.setObjectName("Secondary")
        change.setCursor(Qt.PointingHandCursor)
        change.setFixedHeight(32)
        change.setStyleSheet("min-height: 32px; padding: 0 14px;")
        change.clicked.connect(self._choose_folder)
        fl.addWidget(change)
        lay.addWidget(field)
        lay.addWidget(spacer(6))
        self.space_label = label("", "Small")
        lay.addWidget(self.space_label)
        lay.addWidget(spacer(20))

        self.t_desktop = Toggle(True)
        self.t_start = Toggle(True)
        lay.addWidget(option_row("painel", "Atalho no ambiente de trabalho", "Abrir o SafeOti com um duplo clique.", self.t_desktop))
        lay.addWidget(spacer(8))
        lay.addWidget(option_row("pesquisa", "Atalho no Menu Iniciar", "Encontrar o SafeOti ao pesquisar no Windows.", self.t_start))
        lay.addWidget(spacer(12))
        self.options_error = label("", "Error", wrap=True)
        self.options_error.hide()
        lay.addWidget(self.options_error)
        lay.addStretch(1)
        self._refresh_folder()
        return page

    def _progress_page(self) -> QWidget:
        page = QWidget()
        lay = page_layout(page)
        lay.addWidget(spacer(14))
        self.progress_title = label("A instalar…", "Title")
        lay.addWidget(self.progress_title)
        lay.addWidget(spacer(6))
        self.progress_lead = label("Pode continuar a usar o computador enquanto isto acontece.", "Lead", wrap=True)
        lay.addWidget(self.progress_lead)
        lay.addStretch(1)
        self.percent = label("0%", "Big")
        lay.addWidget(self.percent)
        lay.addWidget(spacer(10))
        self.bar = ProgressLine()
        lay.addWidget(self.bar)
        lay.addWidget(spacer(10))
        self.status = label("A preparar…", "Muted")
        lay.addWidget(self.status)
        lay.addWidget(spacer(14))
        self.install_error = label("", "Error", wrap=True)
        self.install_error.hide()
        lay.addWidget(self.install_error)
        lay.addStretch(2)
        return page

    def _done_page(self) -> QWidget:
        page = QWidget()
        lay = page_layout(page)
        lay.addStretch(1)
        self.mark = SuccessMark(76)
        lay.addWidget(self.mark, 0, Qt.AlignLeft)
        lay.addWidget(spacer(22))
        lay.addWidget(label("O SafeOti está pronto", "Title"))
        lay.addWidget(spacer(6))
        lay.addWidget(label("Ao abrir, o Windows vai pedir confirmação de administrador — "
                            "é necessária para limpar e otimizar o sistema.", "Lead", wrap=True))
        lay.addWidget(spacer(22))
        self.t_launch = Toggle(True)
        lay.addWidget(option_row("arranque", "Abrir o SafeOti agora", "Começar já com uma análise ao PC.", self.t_launch))
        lay.addStretch(2)
        return page

    # ------------------------------------------------------------ navegação
    def _go(self, index: int) -> None:
        self.index = index
        self.set_step(index)
        self.show_page(index)
        self.btn_back.setVisible(index in (1,))
        self.btn_back.setText("Voltar")
        self.btn_next.setEnabled(True)
        self.btn_next.setText({0: "Começar", 1: "Instalar", 2: "A instalar…", 3: "Concluir"}[index])
        if index == 1:
            self.options_error.hide()
        if index == 2:
            self.btn_next.setEnabled(False)
        if index == 3:
            QTimer.singleShot(150, self.mark.play)

    def _back(self) -> None:
        if self.index == 1:
            self._go(0)
        elif self.index == 2:  # depois de um erro: voltar às opções
            self._go(1)

    def _next(self) -> None:
        if self.index == 0:
            self._go(1)
        elif self.index == 1:
            self._start_install()
        elif self.index == 2:  # "Tentar de novo"
            self._start_install()
        elif self.index == 3:
            if self.t_launch.isChecked() and self.exe_path:
                try:
                    os.startfile(self.exe_path)  # ShellExecute: mostra o pedido de UAC
                except OSError:
                    pass
            self.close()

    # ------------------------------------------------------------ pasta
    def _refresh_folder(self) -> None:
        self.folder_label.setText(self.folder)
        size = os.path.getsize(payload_path()) if os.path.isfile(payload_path()) else 0
        free = ops.free_space(self.folder)
        text = f"Espaço necessário: {format_bytes(size)}"
        if free is not None:
            text += f"   ·   Disponível: {format_bytes(free)}"
        self.space_label.setText(text)

    def _choose_folder(self) -> None:
        start = os.path.dirname(self.folder) if os.path.isdir(os.path.dirname(self.folder)) else os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "Escolher a pasta de instalação", start)
        if chosen:
            self.folder = ops.normalize_target(chosen)
            self._refresh_folder()

    # ------------------------------------------------------------ instalar
    def _start_install(self) -> None:
        try:
            ops.check_writable(self.folder)
        except ops.InstallError as exc:
            if self.index != 1:
                self._go(1)
            self.options_error.setText(str(exc))
            self.options_error.show()
            return
        self._go(2)
        self.progress_title.setText("A instalar…")
        self.progress_lead.setText("Pode continuar a usar o computador enquanto isto acontece.")
        self.install_error.hide()
        self.bar.reset()
        self.bar.set_running(True)
        self.percent.setText("0%")
        self.can_close = False
        self.btn_close.setEnabled(False)
        self.btn_back.hide()

        self._thread = QThread(self)
        self._worker = InstallWorker(self.folder, self.t_desktop.isChecked(), self.t_start.isChecked())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        for sig in (self._worker.failed, self._worker.finished):
            sig.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, fraction: float, message: str) -> None:
        self.bar.set_target(fraction)
        self.percent.setText(f"{int(fraction * 100)}%")
        self.status.setText(message)

    def _on_failed(self, message: str) -> None:
        self.can_close = True
        self.btn_close.setEnabled(True)
        self.bar.set_running(False)
        self.progress_title.setText("Não foi possível instalar")
        self.progress_lead.setText("Nada foi alterado de forma permanente. Corrija o problema e tente de novo.")
        self.status.setText("")
        self.install_error.setText(message)
        self.install_error.show()
        self.btn_back.show()
        self.btn_next.setText("Tentar de novo")
        self.btn_next.setEnabled(True)

    def _on_finished(self, exe: str) -> None:
        self.exe_path = exe
        self.bar.set_running(False)
        self.percent.setText("100%")
        self.can_close = True
        self.btn_close.setEnabled(True)
        QTimer.singleShot(450, lambda: self._go(3))


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SafeOti.Setup")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    prepare_app(app)
    app.setWindowIcon(app_icon())
    window = Installer()
    window.appear()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
