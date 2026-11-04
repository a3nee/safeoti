"""Janela principal: barra lateral de navegação, barra superior e páginas."""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core import __version__, admin
from core.i18n import tr
from core.settings import Settings
from ui import icons, workers
from ui.common import (
    button,
    confirm,
    make_app_icon,
    request_admin_relaunch,
    show_error,
    show_info,
    show_warning,
)
from ui.pages.cleaner_page import CleanerPage
from ui.pages.dashboard import DashboardPage
from ui.pages.gaming_page import GamingPage
from ui.pages.processes_page import ProcessesPage
from ui.pages.settings_page import SettingsPage
from ui.pages.startup_page import StartupPage
from ui.workers import MonitorWorker, start_task

log = logging.getLogger("safeoti.ui.main")

NAV_ITEMS = [
    ("Painel", "painel"),
    ("Limpeza", "limpeza"),
    ("Arranque", "arranque"),
    ("Processos", "processos"),
    ("Modo Gaming", "gaming"),
    ("Definições", "definicoes"),
]


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self._is_admin = admin.is_admin()
        self._skip_close_prompts = False
        self._restore_worker = None
        self.setWindowTitle("SafeOti" + ("  (" + tr("Administrador") + ")" if self._is_admin else ""))
        self.setWindowIcon(make_app_icon())
        self.setMinimumSize(QSize(900, 600))
        self.resize(1240, 800)
        self._build()
        self._start_monitor()

    # ------------------------------------------------------------------ UI --
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("Central")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        # Barra lateral
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(240)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(16, 22, 16, 16)
        side_layout.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icons.logo_pixmap(30))
        logo.setFixedSize(30, 30)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand = QLabel("SafeOti")
        brand.setObjectName("Brand")
        version = QLabel(tr("versão {versao}", versao=__version__))
        version.setObjectName("BrandVersion")
        brand_text.addWidget(brand)
        brand_text.addWidget(version)
        brand_row.addWidget(logo, 0, Qt.AlignVCenter)
        brand_row.addLayout(brand_text, 1)
        side_layout.addLayout(brand_row)
        side_layout.addSpacing(22)

        menu_label = QLabel(tr("MENU"))
        menu_label.setObjectName("NavCaption")
        side_layout.addWidget(menu_label)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFocusPolicy(Qt.NoFocus)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setCursor(Qt.PointingHandCursor)
        for name, icon_name in NAV_ITEMS:
            item = QListWidgetItem(icons.nav_icon(icon_name), tr(name))
            item.setSizeHint(QSize(200, 42))
            self.nav.addItem(item)
        side_layout.addWidget(self.nav, 1)

        # Rodapé: estado de administrador e ações globais
        footer = QFrame()
        footer.setObjectName("SidebarFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(12, 12, 12, 12)
        footer_layout.setSpacing(8)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        lock = QLabel()
        lock.setPixmap(icons.pixmap("cadeado_aberto" if self._is_admin else "cadeado", "#111111", 16))
        lock.setFixedSize(16, 16)
        status_text = QVBoxLayout()
        status_text.setSpacing(0)
        self.admin_badge = QLabel(tr("Administrador") if self._is_admin else tr("Modo limitado"))
        self.admin_badge.setObjectName("AdminBadge")
        admin_hint = QLabel(tr("Acesso total") if self._is_admin else tr("Algumas funções bloqueadas"))
        admin_hint.setObjectName("AdminHint")
        status_text.addWidget(self.admin_badge)
        status_text.addWidget(admin_hint)
        status_row.addWidget(lock, 0, Qt.AlignVCenter)
        status_row.addLayout(status_text, 1)
        footer_layout.addLayout(status_row)
        if not self._is_admin:
            relaunch = button(tr("Reiniciar como admin"))
            relaunch.setObjectName("SidebarButton")
            relaunch.setToolTip(tr("Reinicia o SafeOti com privilégios de administrador."))
            relaunch.clicked.connect(lambda: request_admin_relaunch(self))
            footer_layout.addWidget(relaunch)
        side_layout.addWidget(footer)
        side_layout.addSpacing(6)

        self.restore_btn = button(
            tr("Criar ponto de restauro"),
            tooltip=tr("Cria um ponto de restauro do sistema antes de fazer alterações (exige administrador)."),
            icon_name="restauro",
        )
        self.restore_btn.setObjectName("SidebarGhost")
        self.restore_btn.clicked.connect(self._create_restore_point)
        side_layout.addWidget(self.restore_btn)
        root.addWidget(sidebar)

        # Páginas
        self.stack = QStackedWidget()
        self.stack.setObjectName("Stack")
        self.dashboard = DashboardPage(self.settings.monitor_interval_ms)
        self.cleaner = CleanerPage()
        self.startup = StartupPage()
        self.processes = ProcessesPage()
        self.gaming = GamingPage(self.settings.gaming_close_apps)
        self.settings_page = SettingsPage(self.settings)
        self._pages = [self.dashboard, self.cleaner, self.startup, self.processes, self.gaming, self.settings_page]
        for page in self._pages:
            self.stack.addWidget(page)
            page.status_message.connect(self._show_status)
        root.addWidget(self.stack, 1)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.gaming.open_settings.connect(lambda: self.nav.setCurrentRow(5))
        self.settings_page.settings_saved.connect(self._on_settings_saved)

        status = QStatusBar()
        status.setSizeGripEnabled(True)
        self.setStatusBar(status)
        if self._is_admin:
            self._show_status(tr("Pronto. A correr como administrador."))
        else:
            self._show_status(tr("Pronto. Algumas funções exigem privilégios de administrador."))

    def _show_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 15000)

    # ------------------------------------------------------------ monitor --
    def _start_monitor(self) -> None:
        self.monitor = MonitorWorker(self.settings.monitor_interval_ms)
        self.monitor.snapshot.connect(self.dashboard.update_snapshot)
        self.monitor.failed.connect(self._on_monitor_failed)
        self.monitor.start()

    def _on_monitor_failed(self, message: str) -> None:
        self._show_status(tr("Monitor do sistema: {message}", message=message))

    def _on_settings_saved(self, settings: Settings) -> None:
        interval_changed = settings.monitor_interval_ms != self.settings.monitor_interval_ms
        theme_changed = settings.theme != self.settings.theme
        language_changed = settings.language != self.settings.language
        # O idioma não muda a meio da sessão (ficaria metade da interface em cada
        # idioma): fica guardado e é aplicado ao reabrir.
        if theme_changed:
            from PySide6.QtWidgets import QApplication

            from ui import theme

            theme.apply(QApplication.instance(), settings.theme)
        if theme_changed or language_changed:
            notes = []
            if theme_changed:
                notes.append(tr("Gráficos e ícones já desenhados ficam com as cores novas quando reabrir o SafeOti."))
            if language_changed:
                notes.append(tr("O novo idioma será usado quando reabrir o SafeOti."))
            show_info(
                self,
                tr("Aparência"),
                tr("Tema aplicado.") if theme_changed else tr("Idioma guardado."),
                "\n\n".join(notes),
            )
        self.settings = settings
        self.gaming.set_apps(settings.gaming_close_apps)
        if interval_changed:
            self.monitor.set_interval(settings.monitor_interval_ms)
            self.dashboard.set_interval(settings.monitor_interval_ms)

    # --------------------------------------------------- ponto de restauro --
    def _create_restore_point(self) -> None:
        if not admin.is_admin():
            if confirm(
                self,
                tr("Ponto de restauro"),
                tr("Criar um ponto de restauro exige privilégios de administrador."),
                tr("Quer reiniciar o SafeOti como administrador?"),
                yes_text=tr("Reiniciar como administrador"),
                no_text=tr("Cancelar"),
                danger=False,
            ):
                request_admin_relaunch(self)
            return
        if not confirm(
            self,
            tr("Ponto de restauro"),
            tr("Criar agora um ponto de restauro do sistema?"),
            tr("Recomendado antes de alterar o arranque ou limpar ficheiros do sistema. Pode demorar alguns minutos."),
            yes_text=tr("Criar"),
            no_text=tr("Cancelar"),
            danger=False,
        ):
            return
        self.restore_btn.setEnabled(False)
        self.restore_btn.setText(tr("A criar ponto de restauro…"))
        self._show_status(tr("A criar ponto de restauro do sistema…"))
        self._restore_worker = start_task(
            admin.create_restore_point, on_success=self._on_restore_done, on_error=self._on_restore_error
        )

    def _reset_restore_button(self) -> None:
        self.restore_btn.setEnabled(True)
        self.restore_btn.setText(tr("Criar ponto de restauro"))

    def _on_restore_done(self, result: admin.RestorePointResult) -> None:
        self._reset_restore_button()
        self._show_status(result.message)
        if result.ok:
            show_info(self, tr("Ponto de restauro"), result.message)
        else:
            show_warning(self, tr("Ponto de restauro"), result.message)

    def _on_restore_error(self, message: str) -> None:
        self._reset_restore_button()
        show_error(self, tr("Ponto de restauro"), message)

    # -------------------------------------------------------------- fechar --
    def closeEvent(self, event) -> None:  # noqa: N802 - API Qt
        if not self._skip_close_prompts:
            if workers.running_tasks():
                if not confirm(
                    self,
                    tr("Operação em curso"),
                    tr("Há uma operação em curso (por exemplo, uma limpeza)."),
                    tr("Quer cancelá-la e fechar o SafeOti?"),
                    yes_text=tr("Cancelar e fechar"),
                    no_text=tr("Continuar"),
                ):
                    event.ignore()
                    return
            if self.gaming.is_active():
                box = QMessageBox(self)
                box.setWindowTitle(tr("Modo Gaming ativo"))
                box.setIcon(QMessageBox.Question)
                box.setText(tr("O Modo Gaming está ativo."))
                box.setInformativeText(
                    tr(
                        "Quer desativá-lo e repor o plano de energia anterior antes de sair?\n"
                        "Se o mantiver ativo, pode desativá-lo na próxima vez que abrir o SafeOti."
                    )
                )
                restore = box.addButton(tr("Desativar e sair"), QMessageBox.YesRole)
                box.addButton(tr("Manter e sair"), QMessageBox.NoRole)
                cancel = box.addButton(tr("Cancelar"), QMessageBox.RejectRole)
                box.setDefaultButton(restore)
                box.exec()
                clicked = box.clickedButton()
                if clicked is cancel or clicked is None:
                    event.ignore()
                    return
                if clicked is restore:
                    error = self.gaming.deactivate_blocking()
                    if error:
                        show_error(self, tr("Modo Gaming"), tr("Não foi possível repor todas as definições."), error)

        log.info("A fechar o SafeOti.")
        for page in self._pages:
            shutdown = getattr(page, "shutdown", None)
            if shutdown:
                try:
                    shutdown()
                except Exception:  # noqa: BLE001
                    log.exception("Erro ao parar a página %s", type(page).__name__)
        workers.stop_all(timeout_ms=8000)
        event.accept()

