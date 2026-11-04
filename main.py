"""SafeOti — limpar, monitorizar e otimizar o PC (Windows 10/11).

Uso:
    python main.py              abre a janela normalmente
    python main.py --minimizado abre minimizado (usado no arranque com o Windows)
    python main.py --desinstalar abre o desinstalador
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback

# Garante que os pacotes core/ e ui/ são encontrados, mesmo a correr de outra pasta.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# O pyqtgraph deve usar sempre o PySide6 (mesmo que haja PyQt instalado).
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

from core import __version__  # noqa: E402
from core.admin import is_admin  # noqa: E402
from core.i18n import tr  # noqa: E402
from core.settings import load_settings, setup_logging  # noqa: E402

log = logging.getLogger("safeoti")


def _set_windows_app_id() -> None:
    """Faz o Windows mostrar o ícone da app na barra de tarefas (e não o do Python)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SafeOti.App")
    except Exception:  # noqa: BLE001
        pass


def _install_exception_hooks() -> None:
    """Regista exceções não tratadas e mostra uma mensagem em vez de a app terminar."""
    showing = {"active": False}

    def show(exc_type, exc_value, exc_tb) -> None:
        log.critical("Exceção não tratada", exc_info=(exc_type, exc_value, exc_tb))
        if showing["active"]:
            return
        try:
            from PySide6.QtCore import QThread
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None or QThread.currentThread() is not app.thread():
                return  # fora da thread principal: fica apenas no registo
            from ui.common import show_error

            showing["active"] = True
            try:
                show_error(
                    None,
                    tr("Erro inesperado"),
                    tr("Ocorreu um erro inesperado, mas o SafeOti continua a funcionar."),
                    tr(
                        "{tipo}: {valor}\n\nOs detalhes foram guardados no ficheiro de registo.",
                        tipo=exc_type.__name__,
                        valor=exc_value,
                    ),
                )
            finally:
                showing["active"] = False
        except Exception:  # noqa: BLE001
            traceback.print_exception(exc_type, exc_value, exc_tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        log.critical(
            "Exceção não tratada na thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = show
    threading.excepthook = thread_hook


def main() -> int:
    if "--desinstalar" in sys.argv:  # chamado por "Aplicações instaladas" do Windows
        _set_windows_app_id()
        from installer.uninstall import run as run_uninstaller

        return run_uninstaller()

    log_path = setup_logging()
    log.info("SafeOti %s a iniciar (administrador=%s, Python %s)", __version__, is_admin(), sys.version.split()[0])
    _install_exception_hooks()
    _set_windows_app_id()

    from PySide6.QtWidgets import QApplication

    from ui.common import make_app_icon
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("SafeOti")
    app.setApplicationDisplayName("SafeOti")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("SafeOti")
    app.setStyle("Fusion")

    from ui import localization, theme

    settings = load_settings()
    localization.apply(app, settings.language)
    theme.apply(app, settings.theme)
    app.setWindowIcon(make_app_icon())

    window = MainWindow(settings)
    if "--minimizado" in sys.argv:
        window.showMinimized()
    else:
        window.show()
    log.info("Janela principal aberta. Registo em %s", log_path)
    code = app.exec()
    log.info("SafeOti terminado (código %s).", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
