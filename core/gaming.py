"""Modo Gaming: plano de energia de alto desempenho, fecho de apps e prioridade do jogo.

Só usa mecanismos documentados do Windows (powercfg e prioridade de processos).
O estado anterior é guardado em %APPDATA%\\SafeOti\\gaming_state.json antes de
qualquer alteração, para poder ser reposto mesmo depois de a app ser fechada.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

import psutil

from .i18n import tr
from .processes import current_username, is_critical, short_username
from .settings import delete_json_state, normalize_process_name, read_json_state, write_json_state
from .utils import IS_WINDOWS, run_hidden

log = logging.getLogger("safeoti.gaming")

STATE_FILE = "gaming_state.json"

GUID_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
GUID_HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
GUID_ULTIMATE_PERFORMANCE = "e9a42b02-d5df-448d-aa00-03f14749eb61"

_ULTIMATE_NAMES = ("ultimate performance", "desempenho máximo", "máximo desempenho", "rendimiento máximo")
_HIGH_NAMES = ("high performance", "alto desempenho", "alto rendimiento", "elevado desempenho")

_SCHEME_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*\(([^)]*)\)\s*(\*)?"
)


class GamingError(Exception):
    """Erro do Modo Gaming (mensagem pronta a mostrar)."""


@dataclass
class PowerScheme:
    guid: str
    name: str
    active: bool = False


@dataclass
class GamingOptions:
    close_apps: bool = True
    apps: List[str] = field(default_factory=list)
    game_pid: Optional[int] = None
    game_name: str = ""
    game_create_time: Optional[float] = None


@dataclass
class GamingReport:
    active: bool
    changes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class RunningApp:
    pid: int
    name: str


# --------------------------------------------------------------------------- #
# Planos de energia
# --------------------------------------------------------------------------- #
def parse_schemes(output: str) -> List[PowerScheme]:
    schemes: List[PowerScheme] = []
    for match in _SCHEME_RE.finditer(output):
        schemes.append(PowerScheme(match.group(1).lower(), match.group(2).strip(), bool(match.group(3))))
    return schemes


def list_power_schemes() -> List[PowerScheme]:
    if not IS_WINDOWS:
        return []
    result = run_hidden(["powercfg.exe", "/list"], timeout=20)
    if not result.ok:
        raise GamingError(tr("Não foi possível ler os planos de energia: {detail}", detail=result.output))
    return parse_schemes(result.stdout)


def get_active_scheme() -> Optional[PowerScheme]:
    if not IS_WINDOWS:
        return None
    result = run_hidden(["powercfg.exe", "/getactivescheme"], timeout=20)
    if not result.ok:
        return None
    schemes = parse_schemes(result.stdout)
    if schemes:
        schemes[0].active = True
        return schemes[0]
    return None


def choose_performance_scheme(schemes: Iterable[PowerScheme]) -> Optional[PowerScheme]:
    """Escolhe Desempenho Máximo se existir; senão Alto Desempenho."""
    schemes = list(schemes)
    for scheme in schemes:
        if scheme.guid == GUID_ULTIMATE_PERFORMANCE or scheme.name.lower() in _ULTIMATE_NAMES:
            return scheme
    for scheme in schemes:
        if scheme.guid == GUID_HIGH_PERFORMANCE or scheme.name.lower() in _HIGH_NAMES:
            return scheme
    return None


def set_active_scheme(guid: str) -> None:
    result = run_hidden(["powercfg.exe", "/setactive", guid], timeout=20)
    if not result.ok:
        raise GamingError(
            tr("Não foi possível mudar o plano de energia: {detail}", detail=result.output or tr("erro desconhecido"))
        )


# --------------------------------------------------------------------------- #
# Apps em segundo plano
# --------------------------------------------------------------------------- #
def find_running_apps(apps: Iterable[str]) -> List[RunningApp]:
    """Processos do utilizador atual cujo nome está na lista indicada."""
    wanted = {normalize_process_name(a).lower() for a in apps if a and a.strip()}
    wanted.discard("")
    if not wanted:
        return []
    me = current_username().lower()
    found: List[RunningApp] = []
    for proc in psutil.process_iter(["pid", "name", "username"], ad_value=None):
        name = proc.info.get("name") or ""
        pid = proc.info.get("pid")
        if name.lower() not in wanted or is_critical(name, pid):
            continue
        owner = short_username(proc.info.get("username")).lower()
        if me and owner and owner != me:
            continue  # nunca fechar processos de outros utilizadores
        found.append(RunningApp(pid, name))
    return found


def close_apps(apps: Iterable[str], timeout: float = 5.0) -> tuple[List[str], List[str]]:
    """Fecha as apps indicadas. Devolve (nomes fechados, avisos)."""
    running = find_running_apps(apps)
    procs: List[psutil.Process] = []
    warnings: List[str] = []
    for app in running:
        try:
            proc = psutil.Process(app.pid)
            proc.terminate()
            procs.append(proc)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            warnings.append(tr("Sem permissão para fechar {name} (PID {pid}).", name=app.name, pid=app.pid))
    _gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    closed_counts: dict = {}
    for app in running:
        if any(p.pid == app.pid for p in procs):
            closed_counts[app.name] = closed_counts.get(app.name, 0) + 1
    closed = [
        tr("{name} ({count} processos)", name=name, count=count) if count > 1 else tr("{name} (1 processo)", name=name)
        for name, count in sorted(closed_counts.items())
    ]
    for item in closed:
        log.info("Modo Gaming: fechado %s", item)
    for warning in warnings:
        log.warning("Modo Gaming: %s", warning)
    return closed, warnings


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    return read_json_state(STATE_FILE)


def is_active() -> bool:
    return bool(load_state().get("active"))


def _priority_label(value) -> str:
    if not IS_WINDOWS:
        return str(value)
    labels = {
        psutil.IDLE_PRIORITY_CLASS: tr("Baixa"),
        psutil.BELOW_NORMAL_PRIORITY_CLASS: tr("Abaixo do normal"),
        psutil.NORMAL_PRIORITY_CLASS: tr("Normal"),
        psutil.ABOVE_NORMAL_PRIORITY_CLASS: tr("Acima do normal"),
        psutil.HIGH_PRIORITY_CLASS: tr("Alta"),
        psutil.REALTIME_PRIORITY_CLASS: tr("Tempo real"),
    }
    return labels.get(value, str(value))


def _high_priority_value():
    return psutil.HIGH_PRIORITY_CLASS if IS_WINDOWS else -5


def activate(options: GamingOptions) -> GamingReport:
    """Ativa o Modo Gaming e devolve a lista de alterações efetuadas."""
    if is_active():
        raise GamingError(tr("O Modo Gaming já está ativo."))
    if not IS_WINDOWS:
        raise GamingError(tr("O Modo Gaming só está disponível no Windows."))

    report = GamingReport(active=True)
    state: dict = {"active": True, "activated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "changes": []}

    # 1) Plano de energia (guardar o atual ANTES de mudar).
    current = get_active_scheme()
    schemes = list_power_schemes()
    target = choose_performance_scheme(schemes)
    if current:
        state["previous_scheme"] = {"guid": current.guid, "name": current.name}
    write_json_state(STATE_FILE, state)
    log.info("Modo Gaming: a ativar (plano atual: %s)", current.name if current else "desconhecido")

    if target is None:
        # Em portáteis com Modern Standby o Alto Desempenho pode estar oculto; tentar pelo GUID conhecido.
        try:
            set_active_scheme(GUID_HIGH_PERFORMANCE)
            target = PowerScheme(GUID_HIGH_PERFORMANCE, tr("Alto desempenho"))
        except GamingError:
            report.warnings.append(
                tr("Este PC não tem o plano 'Alto desempenho' nem 'Desempenho máximo'; o plano de energia não foi alterado.")
            )
    elif current and current.guid == target.guid:
        report.changes.append(tr("Plano de energia já estava em '{name}' (sem alteração).", name=target.name))
        target = None
    else:
        try:
            set_active_scheme(target.guid)
        except GamingError as exc:
            report.warnings.append(str(exc))
            target = None

    if target is not None:
        before = current.name if current else tr("desconhecido")
        change = tr("Plano de energia: '{before}' → '{new}'", before=before, new=target.name)
        report.changes.append(change)
        state["applied_scheme"] = {"guid": target.guid, "name": target.name}
        log.info("Modo Gaming: %s", change)
    write_json_state(STATE_FILE, state)

    # 2) Fechar apps em segundo plano.
    if options.close_apps and options.apps:
        closed, warnings = close_apps(options.apps)
        report.warnings.extend(warnings)
        if closed:
            report.changes.append(tr("Apps fechadas: {apps}", apps=", ".join(closed)))
            state["closed_apps"] = closed
        else:
            report.changes.append(tr("Nenhuma das apps da lista estava aberta."))

    # 3) Prioridade do jogo.
    if options.game_pid:
        try:
            proc = psutil.Process(options.game_pid)
            if options.game_create_time and abs(proc.create_time() - options.game_create_time) > 0.01:
                raise psutil.NoSuchProcess(options.game_pid)
            if is_critical(proc.name(), proc.pid):
                raise GamingError(tr("Não é permitido alterar a prioridade de processos do sistema."))
            previous = proc.nice()
            proc.nice(_high_priority_value())
            state["priority"] = {
                "pid": proc.pid,
                "name": proc.name(),
                "create_time": proc.create_time(),
                "previous": previous,
            }
            change = tr("Prioridade de {name} (PID {pid}): {previous} → Alta", name=proc.name(), pid=proc.pid, previous=_priority_label(previous))
            report.changes.append(change)
            log.info("Modo Gaming: %s", change)
        except psutil.NoSuchProcess:
            report.warnings.append(tr("O processo do jogo ({game}) já não está em execução.", game=options.game_name or options.game_pid))
        except psutil.AccessDenied:
            report.warnings.append(
                tr("Sem permissão para alterar a prioridade de {game}. Tente reiniciar o SafeOti como administrador.", game=options.game_name or options.game_pid)
            )
        except GamingError as exc:
            report.warnings.append(str(exc))

    state["changes"] = report.changes
    state["warnings"] = report.warnings
    write_json_state(STATE_FILE, state)
    for warning in report.warnings:
        log.warning("Modo Gaming: %s", warning)
    return report


def deactivate() -> GamingReport:
    """Repõe o plano de energia anterior (e a prioridade do jogo, se ainda estiver aberto)."""
    state = load_state()
    report = GamingReport(active=False)
    if not state.get("active"):
        report.changes.append(tr("O Modo Gaming não estava ativo."))
        return report

    previous = state.get("previous_scheme") or {}
    applied = state.get("applied_scheme")
    if previous.get("guid") and applied:
        try:
            set_active_scheme(previous["guid"])
            change = tr("Plano de energia reposto: '{name}'", name=previous.get('name', previous['guid']))
            report.changes.append(change)
            log.info("Modo Gaming: %s", change)
        except GamingError as exc:
            report.warnings.append(tr("{error} Pode repor o plano manualmente em Opções de Energia.", error=exc))
    elif not applied:
        report.changes.append(tr("O plano de energia não tinha sido alterado."))

    priority = state.get("priority")
    if priority:
        try:
            proc = psutil.Process(int(priority["pid"]))
            if abs(proc.create_time() - float(priority.get("create_time", 0))) <= 0.01:
                proc.nice(priority["previous"])
                change = tr("Prioridade de {name} reposta: {priority}", name=priority.get('name'), priority=_priority_label(priority['previous']))
                report.changes.append(change)
                log.info("Modo Gaming: %s", change)
        except psutil.NoSuchProcess:
            pass  # o jogo já foi fechado: nada a repor
        except (psutil.AccessDenied, KeyError, ValueError, TypeError) as exc:
            report.warnings.append(tr("Não foi possível repor a prioridade de {name}: {error}", name=priority.get('name'), error=exc))

    if state.get("closed_apps"):
        report.changes.append(tr("As apps fechadas não são reabertas automaticamente."))
    delete_json_state(STATE_FILE)
    log.info("Modo Gaming desativado.")
    return report
