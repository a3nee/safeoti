"""Página "Definições"."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import __version__, admin, startup
from core.i18n import LANGUAGES, tr
from core.settings import (
    DEFAULT_GAMING_APPS,
    MAX_MONITOR_INTERVAL_MS,
    MIN_MONITOR_INTERVAL_MS,
    Settings,
    app_data_dir,
    logs_dir,
    normalize_process_name,
    save_settings,
)
from ui.common import (
    AdminNotice,
    PageBase,
    StatusLine,
    button,
    confirm,
    page_header,
    section_label,
    show_error,
    show_warning,
)
from ui.common import PAGE_MARGINS, PAGE_SPACING
from ui.update_dialog import check_for_updates
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.settings")


def _open_folder(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path])  # noqa: S603,S607


class SettingsPage(PageBase):
    settings_saved = Signal(object)

    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._worker: Optional[TaskWorker] = None
        self._autostart_actual: Optional[bool] = None
        self._pending: Settings = settings
        self._build()
        self._load_into_widgets(settings)
        self._check_autostart()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("PageContent")
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)
        layout.addWidget(page_header(tr("Definições"), tr("Guardadas em {path}", path=app_data_dir())))

        # Apps do Modo Gaming
        layout.addWidget(section_label(tr("Apps a fechar no Modo Gaming")))
        apps_card = QFrame()
        apps_card.setObjectName("Card")
        apps_layout = QVBoxLayout(apps_card)
        apps_layout.setContentsMargins(16, 14, 16, 14)
        info = QLabel(tr("Nome do executável de cada app (por exemplo, Discord.exe). Só são fechados processos do seu utilizador."))
        info.setObjectName("MutedLabel")
        info.setWordWrap(True)
        apps_layout.addWidget(info)
        self.apps_list = QListWidget()
        self.apps_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.apps_list.setMinimumHeight(160)
        apps_layout.addWidget(self.apps_list)
        add_row = QHBoxLayout()
        self.app_input = QLineEdit()
        self.app_input.setPlaceholderText(tr("ex.: Spotify.exe"))
        self.app_input.returnPressed.connect(self._add_app)
        add_row.addWidget(self.app_input, 1)
        btn_add = button(tr("Adicionar"))
        btn_add.clicked.connect(self._add_app)
        btn_remove = button(tr("Remover selecionadas"))
        btn_remove.clicked.connect(self._remove_apps)
        btn_defaults = button(tr("Repor predefinições"))
        btn_defaults.clicked.connect(self._reset_apps)
        add_row.addWidget(btn_add)
        add_row.addWidget(btn_remove)
        add_row.addWidget(btn_defaults)
        apps_layout.addLayout(add_row)
        layout.addWidget(apps_card)

        # Monitor
        layout.addWidget(section_label(tr("Monitor do sistema")))
        monitor_card = QFrame()
        monitor_card.setObjectName("Card")
        monitor_layout = QHBoxLayout(monitor_card)
        monitor_layout.setContentsMargins(16, 14, 16, 14)
        monitor_layout.addWidget(QLabel(tr("Intervalo de atualização:")))
        self.interval = QSpinBox()
        self.interval.setRange(MIN_MONITOR_INTERVAL_MS, MAX_MONITOR_INTERVAL_MS)
        self.interval.setSingleStep(250)
        self.interval.setSuffix(" ms")
        self.interval.setMinimumWidth(120)
        monitor_layout.addWidget(self.interval)
        monitor_layout.addStretch(1)
        layout.addWidget(monitor_card)

        # Aparência
        layout.addWidget(section_label(tr("Aparência")))
        look_card = QFrame()
        look_card.setObjectName("Card")
        look_layout = QVBoxLayout(look_card)
        look_layout.setContentsMargins(16, 14, 16, 14)
        look_layout.setSpacing(10)
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel(tr("Tema")))
        self.theme_combo = QComboBox()
        for code, label in (("sistema", "Seguir o Windows"), ("claro", "Claro"), ("escuro", "Escuro")):
            self.theme_combo.addItem(tr(label), code)
        self.theme_combo.setMinimumWidth(180)
        theme_row.addWidget(self.theme_combo)
        theme_row.addStretch(1)
        look_layout.addLayout(theme_row)
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(tr("Idioma")))
        self.lang_combo = QComboBox()
        for code, label in LANGUAGES:
            self.lang_combo.addItem(label, code)
        self.lang_combo.setMinimumWidth(180)
        lang_row.addWidget(self.lang_combo)
        lang_row.addStretch(1)
        look_layout.addLayout(lang_row)
        look_info = QLabel(tr("O tema muda logo ao guardar. O novo idioma aplica-se ao reabrir o SafeOti."))
        look_info.setObjectName("MutedLabel")
        look_info.setWordWrap(True)
        look_layout.addWidget(look_info)
        layout.addWidget(look_card)

        # Arranque
        layout.addWidget(section_label(tr("Arranque")))
        layout.addWidget(AdminNotice(tr("Para iniciar o SafeOti com o Windows são necessários privilégios de administrador.")))
        startup_card = QFrame()
        startup_card.setObjectName("Card")
        startup_layout = QVBoxLayout(startup_card)
        startup_layout.setContentsMargins(16, 14, 16, 14)
        self.chk_autostart = QCheckBox(tr("Iniciar o SafeOti com o Windows (minimizado)"))
        startup_layout.addWidget(self.chk_autostart)
        self.autostart_info = QLabel(tr("A verificar o estado atual…"))
        self.autostart_info.setObjectName("MutedLabel")
        self.autostart_info.setWordWrap(True)
        startup_layout.addWidget(self.autostart_info)
        layout.addWidget(startup_card)

        # Atualizações
        layout.addWidget(section_label(tr("Atualizações")))
        updates_card = QFrame()
        updates_card.setObjectName("Card")
        updates_layout = QVBoxLayout(updates_card)
        updates_layout.setContentsMargins(16, 14, 16, 14)
        self.chk_updates = QCheckBox(tr("Procurar atualizações ao abrir o SafeOti"))
        updates_layout.addWidget(self.chk_updates)
        updates_row = QHBoxLayout()
        version_label = QLabel(tr("Versão {versao}", versao=__version__))
        version_label.setObjectName("MutedLabel")
        updates_row.addWidget(version_label)
        updates_row.addStretch(1)
        self.btn_updates = button(tr("Procurar atualizações"), icon_name="atualizar")
        self.btn_updates.clicked.connect(self._check_updates)
        updates_row.addWidget(self.btn_updates)
        updates_layout.addLayout(updates_row)
        layout.addWidget(updates_card)

        # Registos
        layout.addWidget(section_label(tr("Registos")))
        logs_card = QFrame()
        logs_card.setObjectName("Card")
        logs_layout = QHBoxLayout(logs_card)
        logs_layout.setContentsMargins(16, 14, 16, 14)
        logs_label = QLabel(tr("Todas as ações ficam registadas em {path}", path=logs_dir()))
        logs_label.setWordWrap(True)
        logs_label.setObjectName("MutedLabel")
        logs_layout.addWidget(logs_label, 1)
        btn_logs = button(tr("Abrir pasta de registos"), icon_name="pasta")
        btn_logs.clicked.connect(self._open_logs)
        logs_layout.addWidget(btn_logs)
        layout.addWidget(logs_card)

        layout.addStretch(1)
        self.status = StatusLine()
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.btn_discard = button(tr("Descartar alterações"))
        self.btn_discard.clicked.connect(lambda: self._load_into_widgets(self._settings))
        self.btn_save = button(tr("Guardar definições"), "primary")
        self.btn_save.clicked.connect(self._save)
        actions.addWidget(self.btn_discard)
        actions.addWidget(self.btn_save)
        layout.addLayout(actions)

    # --------------------------------------------------------------- dados --
    def _load_into_widgets(self, settings: Settings) -> None:
        self.apps_list.clear()
        self.apps_list.addItems(settings.gaming_close_apps)
        self.interval.setValue(settings.monitor_interval_ms)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings.theme)))
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(settings.language)))
        self.chk_updates.setChecked(bool(settings.check_updates))
        checked = self._autostart_actual if self._autostart_actual is not None else settings.start_with_windows
        self.chk_autostart.setChecked(bool(checked))
        self.status.set_message("")

    def _check_autostart(self) -> None:
        self._worker = start_task(startup.is_self_autostart_enabled, on_success=self._on_autostart_state,
                                  on_error=self._on_autostart_error)

    def _on_autostart_state(self, enabled: bool) -> None:
        self._autostart_actual = bool(enabled)
        self.chk_autostart.setChecked(bool(enabled))
        self.autostart_info.setText(
            tr("Ativo: o SafeOti inicia ao iniciar sessão (tarefa agendada 'SafeOti').") if enabled
            else tr("Desativado.")
        )

    def _on_autostart_error(self, message: str) -> None:
        self.autostart_info.setText(tr("Não foi possível verificar: {message}", message=message))

    def _current_apps(self):
        return [self.apps_list.item(i).text() for i in range(self.apps_list.count())]

    def _add_app(self) -> None:
        name = normalize_process_name(self.app_input.text())
        if not name:
            return
        existing = {a.lower() for a in self._current_apps()}
        if name.lower() in existing:
            self.status.set_message(tr("'{name}' já está na lista.", name=name), "warning")
        else:
            from core.processes import is_critical

            if is_critical(name, -1) or name.lower() in ("explorer.exe", "safeoti.exe"):
                show_warning(self, tr("Definições"), tr("'{name}' não pode ser adicionado: é um processo do sistema.", name=name))
                return
            self.apps_list.addItem(name)
            self.status.set_message(tr("'{name}' adicionado. Carregue em 'Guardar definições' para aplicar.", name=name))
        self.app_input.clear()

    def _remove_apps(self) -> None:
        for item in self.apps_list.selectedItems():
            self.apps_list.takeItem(self.apps_list.row(item))

    def _reset_apps(self) -> None:
        if confirm(self, tr("Repor predefinições"), tr("Substituir a lista atual pela lista predefinida?"),
                   ", ".join(DEFAULT_GAMING_APPS), yes_text=tr("Repor"), no_text=tr("Cancelar"), danger=False):
            self.apps_list.clear()
            self.apps_list.addItems(DEFAULT_GAMING_APPS)

    def _open_logs(self) -> None:
        try:
            _open_folder(str(logs_dir()))
        except OSError as exc:
            show_error(self, tr("Registos"), tr("Não foi possível abrir a pasta de registos."), str(exc))

    def _save(self) -> None:
        new = replace(
            self._settings,
            gaming_close_apps=self._current_apps(),
            monitor_interval_ms=self.interval.value(),
            start_with_windows=self.chk_autostart.isChecked(),
            theme=self.theme_combo.currentData(),
            language=self.lang_combo.currentData(),
            check_updates=self.chk_updates.isChecked(),
        ).validate()

        wanted_autostart = new.start_with_windows
        current_autostart = self._autostart_actual
        if current_autostart is None:
            current_autostart = self._settings.start_with_windows
        if wanted_autostart != current_autostart:
            if not admin.is_admin():
                show_warning(
                    self, tr("Iniciar com o Windows"),
                    tr("Alterar o arranque automático exige privilégios de administrador."),
                    tr("As restantes definições vão ser guardadas. Reinicie o SafeOti como administrador para mudar esta opção."),
                )
                new.start_with_windows = current_autostart
                self.chk_autostart.setChecked(current_autostart)
            else:
                self.btn_save.setEnabled(False)
                self.status.set_message(tr("A configurar o arranque automático…"))
                self._pending = new
                self._worker = start_task(startup.set_self_autostart, wanted_autostart,
                                          on_success=self._on_autostart_saved, on_error=self._on_autostart_save_error)
                return
        self._persist(new)

    def _on_autostart_saved(self, _result) -> None:
        self.btn_save.setEnabled(True)
        self._autostart_actual = self._pending.start_with_windows
        self._on_autostart_state(self._autostart_actual)
        self._persist(self._pending)

    def _on_autostart_save_error(self, message: str) -> None:
        self.btn_save.setEnabled(True)
        show_error(self, tr("Iniciar com o Windows"), message)
        current = bool(self._autostart_actual)
        self._pending.start_with_windows = current
        self.chk_autostart.setChecked(current)
        self._persist(self._pending)

    def _persist(self, new: Settings) -> None:
        try:
            save_settings(new)
        except OSError as exc:
            log.exception("Erro ao guardar definições")
            show_error(self, tr("Definições"), tr("Não foi possível guardar as definições."), str(exc))
            self.status.set_message(tr("Erro ao guardar as definições."), "error")
            return
        self._settings = new
        self.status.set_message(tr("Definições guardadas."), "success")
        self.status_message.emit(tr("Definições guardadas."))
        self.settings_saved.emit(new)

    # ------------------------------------------------------- atualizações --
    def _check_updates(self) -> None:
        self.btn_updates.setEnabled(False)
        check_for_updates(
            self,
            self._settings,
            manual=True,
            on_done=lambda: self.btn_updates.setEnabled(True),
        )

    def shutdown(self) -> None:
        pass

