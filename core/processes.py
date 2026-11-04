"""Gestor de processos: recolha de dados, término seguro e localização do executável."""

from __future__ import annotations

import getpass
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import psutil

from .i18n import tr
from .utils import IS_WINDOWS

log = logging.getLogger("safeoti.processes")

# Processos cujo término é bloqueado (podem causar ecrã azul, terminar a sessão ou instabilidade).
CRITICAL_PROCESSES = {
    "system",
    "system idle process",
    "registry",
    "memory compression",
    "secure system",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
}
CRITICAL_PIDS = {0, 4}

# Processos que podem ser terminados, mas só depois de um aviso específico.
WARN_PROCESSES = {
    "explorer.exe": (
        "Terminar o explorer.exe faz desaparecer a barra de tarefas, o menu Iniciar e o ambiente de trabalho "
        "até o Explorador ser reiniciado (Ctrl+Shift+Esc > Executar nova tarefa > explorer.exe)."
    ),
}


class ProcessError(Exception):
    """Erro ao atuar sobre um processo (mensagem pronta a mostrar)."""


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float
    memory_bytes: int
    username: str
    exe: str
    create_time: float


def is_critical(name: str, pid: int) -> bool:
    return pid in CRITICAL_PIDS or (name or "").lower() in CRITICAL_PROCESSES or pid == os.getpid()


def critical_reason(name: str, pid: int) -> str:
    if pid == os.getpid():
        return tr("Este é o próprio SafeOti. Para o fechar, use o botão de fechar da janela.")
    return (
        tr("O processo '{name}' é essencial ao funcionamento do Windows. Terminá-lo pode bloquear o PC, terminar a sessão ou provocar um ecrã azul, por isso o SafeOti não permite esta ação.", name=name)
    )


def warning_for(name: str) -> Optional[str]:
    return WARN_PROCESSES.get((name or "").lower())


def current_username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME", "")


def short_username(full: Optional[str]) -> str:
    """'DOMINIO\\utilizador' -> 'utilizador'."""
    if not full:
        return ""
    return full.split("\\")[-1]


class ProcessSampler:
    """Recolhe a lista de processos mantendo objetos psutil entre chamadas.

    A percentagem de CPU de um processo só é válida a partir da segunda leitura,
    por isso os objetos são reutilizados (identificados por PID + hora de criação).
    """

    def __init__(self) -> None:
        self._cache: Dict[Tuple[int, float], psutil.Process] = {}
        self._cpu_count = psutil.cpu_count(logical=True) or 1

    def sample(self) -> List[ProcessInfo]:
        result: List[ProcessInfo] = []
        seen = set()
        attrs = ["pid", "name", "username", "memory_info", "exe", "create_time"]
        for proc in psutil.process_iter(attrs, ad_value=None):
            info = proc.info
            pid = info.get("pid")
            create_time = info.get("create_time") or 0.0
            key = (pid, create_time)
            seen.add(key)
            cached = self._cache.get(key)
            if cached is None:
                self._cache[key] = proc
                cached = proc
            try:
                cpu = cached.cpu_percent(None) / self._cpu_count
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                cpu = 0.0
            mem = info.get("memory_info")
            name = info.get("name") or ""
            if pid == 0 and not name:
                name = "System Idle Process"
            result.append(
                ProcessInfo(
                    pid=pid,
                    name=name,
                    cpu_percent=max(0.0, cpu),
                    memory_bytes=getattr(mem, "rss", 0) if mem else 0,
                    username=short_username(info.get("username")),
                    exe=info.get("exe") or "",
                    create_time=create_time,
                )
            )
        for key in list(self._cache):
            if key not in seen:
                del self._cache[key]
        return result


def _get_process(pid: int, expected_create_time: Optional[float]) -> psutil.Process:
    try:
        proc = psutil.Process(pid)
        if expected_create_time and abs(proc.create_time() - expected_create_time) > 0.01:
            raise ProcessError(tr("O processo já terminou (o PID pertence agora a outro processo)."))
        return proc
    except psutil.NoSuchProcess as exc:
        raise ProcessError(tr("O processo já terminou.")) from exc
    except psutil.AccessDenied as exc:
        raise ProcessError(tr("Sem permissão para aceder a este processo. Tente como administrador.")) from exc


def terminate_process(pid: int, expected_name: str, expected_create_time: Optional[float] = None,
                      timeout: float = 5.0) -> str:
    """Termina um processo (após as verificações de segurança). Devolve uma mensagem de sucesso."""
    proc = _get_process(pid, expected_create_time)
    try:
        name = proc.name()
    except psutil.Error:
        name = expected_name
    if is_critical(name, pid):
        log.warning("Bloqueado o término do processo crítico %s (PID %d).", name, pid)
        raise ProcessError(critical_reason(name, pid))
    if expected_name and name.lower() != expected_name.lower():
        raise ProcessError(tr("O processo selecionado mudou entretanto. Atualize a lista e tente novamente."))
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied as exc:
        log.warning("Sem permissão para terminar %s (PID %d).", name, pid)
        raise ProcessError(
            tr("Sem permissão para terminar '{name}'. Pode pertencer a outro utilizador ou ao sistema; tente reiniciar o SafeOti como administrador.", name=name)
        ) from exc
    except psutil.TimeoutExpired as exc:
        raise ProcessError(tr("O processo '{name}' não respondeu ao pedido para terminar.", name=name)) from exc
    log.info("Processo terminado: %s (PID %d)", name, pid)
    return tr("O processo '{name}' (PID {pid}) foi terminado.", name=name, pid=pid)


def open_executable_folder(exe_path: str) -> None:
    """Abre o Explorador com o executável selecionado."""
    if not exe_path or not os.path.isfile(exe_path):
        raise ProcessError(tr("Não foi possível determinar a localização do executável deste processo."))
    path = os.path.normpath(exe_path)
    if IS_WINDOWS:
        subprocess.Popen(["explorer.exe", "/select,", path])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])  # noqa: S603,S607
    log.info("Aberta a pasta do executável: %s", path)


def list_user_processes() -> List[ProcessInfo]:
    """Processos do utilizador atual com executável conhecido (para escolher o jogo)."""
    me = current_username().lower()
    result: Dict[str, ProcessInfo] = {}
    for proc in psutil.process_iter(["pid", "name", "username", "exe", "create_time"], ad_value=None):
        info = proc.info
        name = info.get("name") or ""
        if not name or not info.get("exe") or is_critical(name, info["pid"]):
            continue
        if me and short_username(info.get("username")).lower() != me:
            continue
        result.setdefault(f"{name.lower()}:{info['pid']}", ProcessInfo(
            pid=info["pid"], name=name, cpu_percent=0.0, memory_bytes=0,
            username=short_username(info.get("username")), exe=info["exe"],
            create_time=info.get("create_time") or 0.0,
        ))
    return sorted(result.values(), key=lambda p: (p.name.lower(), p.pid))
