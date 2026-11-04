"""Threads de trabalho (QThread) para que a janela nunca congele."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Set

from PySide6.QtCore import QThread, Signal

from core.gaming import GamingError
from core.i18n import tr
from core.monitor import SystemMonitor
from core.processes import ProcessError, ProcessSampler
from core.startup import StartupError, StartupPermissionError
from core.utils import OperationCancelled

log = logging.getLogger("safeoti.ui.workers")

_USER_FACING_ERRORS = (GamingError, ProcessError, StartupError, StartupPermissionError)

_ACTIVE: Set[QThread] = set()


def friendly_error(exc: BaseException) -> str:
    """Converte uma exceção numa mensagem compreensível."""
    if isinstance(exc, _USER_FACING_ERRORS):
        return str(exc)
    if isinstance(exc, PermissionError):
        return tr("Sem permissão para realizar esta operação. Tente reiniciar o SafeOti como administrador.")
    if isinstance(exc, FileNotFoundError):
        return tr("Ficheiro ou pasta não encontrado: {nome}", nome=exc.filename or exc)
    if isinstance(exc, OSError):
        return tr("Erro do sistema: {erro}", erro=exc.strerror or exc)
    return tr("Ocorreu um erro inesperado: {erro}", erro=exc)


def _track(thread: QThread) -> None:
    """Mantém uma referência Python à thread até ela terminar por completo.

    As threads já terminadas são removidas aqui (na thread principal), e não no
    sinal ``finished``: destruir o objeto QThread a partir da própria thread
    ainda em execução faria a aplicação terminar abruptamente.
    """
    for old in list(_ACTIVE):
        if old.isFinished():
            _ACTIVE.discard(old)
    _ACTIVE.add(thread)


def running_tasks() -> list:
    return [t for t in _ACTIVE if isinstance(t, TaskWorker) and t.isRunning()]


def stop_all(timeout_ms: int = 5000) -> None:
    """Pede a todas as threads para pararem e espera por elas (ao fechar a app)."""
    for thread in list(_ACTIVE):
        stop = getattr(thread, "stop", None) or getattr(thread, "cancel", None)
        if stop:
            stop()
    for thread in list(_ACTIVE):
        if thread.isRunning() and not thread.wait(timeout_ms):
            log.warning("Thread %s não terminou a tempo.", thread.objectName() or thread)


class TaskWorker(QThread):
    """Executa uma função numa thread separada.

    Se ``with_progress`` for True, a função recebe os argumentos ``progress``
    (callback percentagem/mensagem) e ``should_cancel`` (função sem argumentos).
    """

    progress = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: Callable[..., Any], *args: Any, with_progress: bool = False, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._with_progress = with_progress
        self._cancel = threading.Event()
        self.setObjectName(getattr(fn, "__name__", "tarefa"))
        _track(self)

    def cancel(self) -> None:
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        kwargs = dict(self._kwargs)
        if self._with_progress:
            kwargs["progress"] = lambda pct, msg: self.progress.emit(int(pct), str(msg))
            kwargs["should_cancel"] = self._cancel.is_set
        try:
            result = self._fn(*self._args, **kwargs)
        except OperationCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:  # noqa: BLE001 - nenhuma exceção pode derrubar a app
            if not isinstance(exc, _USER_FACING_ERRORS):
                log.exception("Erro na tarefa %s", self.objectName())
            else:
                log.warning("Tarefa %s: %s", self.objectName(), exc)
            self.failed.emit(friendly_error(exc))
            return
        self.succeeded.emit(result)


def start_task(fn: Callable[..., Any], *args: Any, on_success=None, on_error=None, on_progress=None,
               on_cancel=None, with_progress: bool = False, **kwargs: Any) -> TaskWorker:
    """Cria, liga e arranca um TaskWorker."""
    worker = TaskWorker(fn, *args, with_progress=with_progress, **kwargs)
    if on_success:
        worker.succeeded.connect(on_success)
    if on_error:
        worker.failed.connect(on_error)
    if on_progress:
        worker.progress.connect(on_progress)
    if on_cancel:
        worker.cancelled.connect(on_cancel)
    worker.start()
    return worker


class MonitorWorker(QThread):
    """Recolhe métricas do sistema a intervalos regulares."""

    snapshot = Signal(object)
    failed = Signal(str)

    def __init__(self, interval_ms: int = 1000) -> None:
        super().__init__()
        self.setObjectName("monitor")
        self._interval = max(0.25, interval_ms / 1000)
        self._stop = threading.Event()
        _track(self)

    def set_interval(self, interval_ms: int) -> None:
        self._interval = max(0.25, interval_ms / 1000)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        monitor = None
        errors = 0
        try:
            monitor = SystemMonitor()
            while not self._stop.wait(self._interval):
                try:
                    self.snapshot.emit(monitor.sample())
                    errors = 0
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    log.exception("Erro ao recolher métricas")
                    if errors in (1, 10):
                        self.failed.emit(friendly_error(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Monitor do sistema falhou ao iniciar")
            self.failed.emit(friendly_error(exc))
        finally:
            if monitor is not None:
                monitor.close()


class ProcessWorker(QThread):
    """Recolhe a lista de processos a cada 2 segundos (só enquanto ativo)."""

    processes = Signal(object)
    failed = Signal(str)

    def __init__(self, interval_ms: int = 2000) -> None:
        super().__init__()
        self.setObjectName("processos")
        self._interval = interval_ms / 1000
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._active = threading.Event()
        _track(self)

    def set_active(self, active: bool) -> None:
        if active:
            self._active.set()
            self._wake.set()
        else:
            self._active.clear()

    def refresh_now(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._active.set()
        self._wake.set()

    def run(self) -> None:
        sampler = ProcessSampler()
        try:
            sampler.sample()  # primeira leitura de CPU (valores começam a 0)
        except Exception:  # noqa: BLE001
            log.exception("Erro na primeira recolha de processos")
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            if not self._active.is_set():
                self._active.wait()
                continue
            try:
                self.processes.emit(sampler.sample())
            except Exception as exc:  # noqa: BLE001
                log.exception("Erro ao recolher processos")
                self.failed.emit(friendly_error(exc))
