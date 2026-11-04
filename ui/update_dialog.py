"""Janela de atualização (GitHub Releases) e verificação de novas versões."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core import updater
from core.i18n import tr
from ui.common import StatusLine, button, confirm, show_error, show_info
from ui.workers import TaskWorker, start_task

log = logging.getLogger("safeoti.ui.updater")

_CHECK_RUNNING = False


def _check_task():
    """Executa a verificação; devolve o resultado ou a UpdateError (mensagem limpa)."""
    try:
        return updater.check_latest()
    except updater.UpdateError as exc:
        return exc


def _download_task(release: updater.ReleaseInfo, progress, should_cancel):
    try:
        return updater.download_release(release, progress, should_cancel)
    except updater.UpdateError as exc:
        return exc


def _save(settings, on_settings_changed: Optional[Callable[[], None]]) -> None:
    if on_settings_changed is not None:
        on_settings_changed()
        return
    try:
        from core.settings import save_settings

        save_settings(settings)
    except Exception:  # noqa: BLE001
        log.exception("Não foi possível guardar as definições.")


def _release_url(release: Optional[updater.ReleaseInfo]) -> str:
    url = release.html_url if release else ""
    if url.startswith("https://github.com/"):
        return url
    return updater.releases_page()


class UpdateDialog(QDialog):
    """Mostra a nova versão, as notas e permite transferir e instalar.

    Depois de ``exec()``: se ``install_now`` for True, ``downloaded_path`` tem o
    instalador transferido e o utilizador aceitou fechar o SafeOti para o abrir.
    """

    def __init__(self, result: updater.CheckResult, settings=None,
                 on_settings_changed: Optional[Callable[[], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._result = result
        self._release = result.release
        self._settings = settings
        self._on_settings_changed = on_settings_changed
        self._worker: Optional[TaskWorker] = None
        self.downloaded_path = ""
        self.install_now = False

        release = self._release
        version = release.version if release else ""
        self.setWindowTitle(tr("Atualização disponível"))
        self.setMinimumSize(560, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)

        title = QLabel(tr("Está disponível o SafeOti {versao}", versao=version))
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        info = tr("Versão instalada: {atual}  ·  Nova versão: {nova}", atual=result.current, nova=version)
        date = (release.published_at or "")[:10] if release else ""
        if date:
            info += "  ·  " + tr("Publicada em {data}", data=date)
        subtitle = QLabel(info)
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        notes_label = QLabel(tr("Novidades"))
        notes_label.setObjectName("SectionTitle")
        layout.addWidget(notes_label)

        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        notes = (release.notes or "").strip() if release else ""
        if notes:
            self.notes.setMarkdown(notes)
        else:
            self.notes.setPlainText(tr("Esta versão não tem notas publicadas."))
        layout.addWidget(self.notes, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status = StatusLine()
        layout.addWidget(self.status)
        if release is not None and release.asset is None:
            self.status.set_message(
                tr("Esta versão não inclui um ficheiro para Windows. Pode transferi-la manualmente no GitHub."),
                "warning",
            )

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_install = button(tr("Transferir e instalar"), "primary")
        self.btn_github = button(tr("Ver no GitHub"))
        self.btn_skip = button(tr("Ignorar esta versão"))
        self.btn_later = button(tr("Mais tarde"))
        self.btn_cancel = button(tr("Cancelar transferência"))
        self.btn_cancel.setVisible(False)
        self.btn_install.setEnabled(release is not None and release.asset is not None)
        self.btn_skip.setEnabled(release is not None and settings is not None)
        buttons.addWidget(self.btn_github)
        buttons.addWidget(self.btn_skip)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_later)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_install)
        layout.addLayout(buttons)

        self.btn_install.clicked.connect(self._start_download)
        self.btn_github.clicked.connect(self._open_github)
        self.btn_skip.clicked.connect(self._skip_version)
        self.btn_later.clicked.connect(self.reject)
        self.btn_cancel.clicked.connect(self._cancel_download)
        self.btn_install.setDefault(True)

    # ------------------------------------------------------------ ações --
    def _open_github(self) -> None:
        QDesktopServices.openUrl(QUrl(_release_url(self._release)))

    def _skip_version(self) -> None:
        if self._release is None or self._settings is None:
            return
        self._settings.skipped_version = self._release.version
        _save(self._settings, self._on_settings_changed)
        log.info("Versão %s ignorada pelo utilizador.", self._release.version)
        self.reject()

    def _set_downloading(self, active: bool) -> None:
        self.progress.setVisible(active or self.progress.value() > 0)
        self.btn_install.setVisible(not active)
        self.btn_cancel.setVisible(active)
        self.btn_cancel.setEnabled(active)
        self.btn_skip.setEnabled(not active and self._settings is not None)
        self.btn_later.setEnabled(not active)

    def _start_download(self) -> None:
        if self._release is None or self._release.asset is None or self._worker is not None:
            return
        self.progress.setValue(0)
        self.progress.setRange(0, 100)
        self.status.set_message(tr("A preparar a transferência…"))
        self._set_downloading(True)
        self._worker = start_task(
            _download_task,
            self._release,
            with_progress=True,
            on_success=self._on_download_done,
            on_error=self._on_download_error,
            on_progress=self._on_progress,
            on_cancel=self._on_download_cancelled,
        )

    def _cancel_download(self) -> None:
        if self._worker is not None:
            self.btn_cancel.setEnabled(False)
            self.status.set_message(tr("A cancelar…"), "warning")
            self._worker.cancel()

    def _on_progress(self, pct: int, message: str) -> None:
        self.progress.setValue(max(0, min(100, pct)))
        if message:
            self.status.set_message(message)

    def _on_download_error(self, message: str) -> None:
        self._worker = None
        self._set_downloading(False)
        self.status.set_message(message, "error")

    def _on_download_cancelled(self) -> None:
        self._worker = None
        self.progress.setValue(0)
        self._set_downloading(False)
        self.status.set_message(tr("Transferência cancelada."), "warning")

    def _on_download_done(self, result) -> None:
        self._worker = None
        if isinstance(result, Exception):
            self._on_download_error(str(result))
            return
        self.downloaded_path = str(result)
        self.progress.setValue(100)
        self._set_downloading(False)
        self.btn_install.setEnabled(False)
        self.status.set_message(tr("Transferência concluída e verificada."), "success")
        if confirm(
            self,
            tr("Instalar atualização"),
            tr("O SafeOti vai fechar para instalar a versão {versao}.", versao=self._release.version),
            tr("O instalador abre a seguir. Fechar o SafeOti agora?"),
            yes_text=tr("Fechar e instalar"),
            no_text=tr("Agora não"),
            danger=False,
        ):
            self.install_now = True
            self.accept()
        else:
            self.status.set_message(
                tr("O instalador ficou guardado em {caminho}", caminho=self.downloaded_path), "info"
            )

    # ---------------------------------------------------------- fechar --
    def reject(self) -> None:  # noqa: D401 - API Qt
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(3000)
            self._worker = None
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - API Qt
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(3000)
            self._worker = None
        super().closeEvent(event)


def install_and_quit(parent: Optional[QWidget], path: str) -> bool:
    """Fecha a janela principal (com as confirmações habituais) e abre o instalador.

    Devolve False se o utilizador cancelar o fecho ou se o instalador não abrir.
    """
    window = parent.window() if parent is not None else None
    if window is not None and window.isVisible() and not window.close():
        return False  # o utilizador cancelou o fecho (operação em curso, Modo Gaming…)
    try:
        updater.launch_installer(path)
    except (updater.UpdateError, OSError) as exc:
        log.exception("Não foi possível abrir o instalador %s", path)
        message = str(exc) if isinstance(exc, updater.UpdateError) else tr("Erro do sistema: {erro}", erro=exc)
        show_error(None, tr("Atualização"), tr("Não foi possível abrir o instalador."), message)
        return False
    log.info("Instalador %s aberto; a terminar o SafeOti.", path)
    QApplication.quit()
    return True


def show_update_dialog(parent: Optional[QWidget], result: updater.CheckResult, settings=None,
                       on_settings_changed: Optional[Callable[[], None]] = None,
                       on_install_ready: Optional[Callable[[str], None]] = None) -> None:
    dialog = UpdateDialog(result, settings, on_settings_changed, parent)
    dialog.exec()
    path, install_now = dialog.downloaded_path, dialog.install_now
    dialog.deleteLater()
    if install_now and path:
        if on_install_ready is not None:
            on_install_ready(path)
        else:
            install_and_quit(parent, path)


def check_for_updates(parent: Optional[QWidget], settings, manual: bool,
                      on_settings_changed: Optional[Callable[[], None]] = None, *,
                      on_done: Optional[Callable[[], None]] = None,
                      on_install_ready: Optional[Callable[[str], None]] = None) -> Optional[TaskWorker]:
    """Procura uma nova versão em segundo plano.

    - ``manual=False`` (arranque): só faz algo se ``settings.check_updates`` for
      True; fica em silêncio quando não há novidades, sem Internet ou em erro, e
      respeita ``settings.skipped_version``.
    - ``manual=True`` (botão "Procurar atualizações"): informa sempre o resultado.

    ``on_settings_changed()`` (sem argumentos) é chamado depois de alterar
    ``settings.skipped_version``; se for None, as definições são guardadas aqui.
    ``on_done()`` é chamado quando a verificação termina (útil para reativar o botão).
    Devolve o TaskWorker, ou None se já houver uma verificação em curso / estiver desativada.
    """
    global _CHECK_RUNNING
    if not manual and settings is not None and not getattr(settings, "check_updates", True):
        if on_done:
            on_done()
        return None
    if _CHECK_RUNNING:
        if on_done:
            on_done()
        return None
    _CHECK_RUNNING = True

    def finish() -> None:
        global _CHECK_RUNNING
        _CHECK_RUNNING = False
        if on_done:
            on_done()

    def handle(result) -> None:
        finish()
        if isinstance(result, Exception):
            log.info("Verificação de atualizações falhou: %s", result)
            if manual:
                show_error(parent, tr("Procurar atualizações"), tr("Não foi possível procurar atualizações."), str(result))
            return
        if result.status == updater.STATUS_NO_RELEASES:
            if manual:
                show_info(parent, tr("Procurar atualizações"), tr("Ainda não há versões publicadas."))
            return
        skipped = getattr(settings, "skipped_version", "") if settings is not None else ""
        if not updater.should_notify(result, skipped, manual):
            if manual:
                show_info(parent, tr("Procurar atualizações"),
                          tr("Já tem a versão mais recente ({versao}).", versao=result.current))
            return
        if manual and settings is not None and getattr(settings, "skipped_version", ""):
            # Procurar manualmente volta a mostrar versões ignoradas; limpa a escolha.
            settings.skipped_version = ""
            _save(settings, on_settings_changed)
        show_update_dialog(parent, result, settings, on_settings_changed, on_install_ready)

    def handle_error(message: str) -> None:
        finish()
        log.warning("Verificação de atualizações falhou: %s", message)
        if manual:
            show_error(parent, tr("Procurar atualizações"), tr("Não foi possível procurar atualizações."), message)

    return start_task(_check_task, on_success=handle, on_error=handle_error)
