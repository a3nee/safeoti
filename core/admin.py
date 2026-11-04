"""Privilégios de administrador e pontos de restauro do sistema."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import List, Optional

from .i18n import tr
from .utils import IS_WINDOWS, run_hidden

log = logging.getLogger("safeoti.admin")


def is_admin() -> bool:
    """Indica se o processo atual está a correr com privilégios de administrador."""
    if not IS_WINDOWS:
        try:
            return os.geteuid() == 0  # type: ignore[attr-defined]
        except AttributeError:
            return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - qualquer falha significa "não sabemos" -> não admin
        return False


def _relaunch_command(extra_args: Optional[List[str]] = None) -> tuple[str, str]:
    """Devolve (executável, parâmetros) para voltar a lançar a aplicação."""
    import subprocess

    args = [a for a in sys.argv[1:] if a != "--minimizado"]
    if extra_args:
        args.extend(extra_args)
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(args)
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.exists(pythonw):
        python = pythonw  # evita abrir uma janela de consola
    script = os.path.abspath(sys.argv[0])
    return python, subprocess.list2cmdline([script, *args])


def relaunch_as_admin() -> bool:
    """Lança uma nova instância elevada (pedido UAC).

    Devolve True se o Windows aceitou o pedido (a instância atual deve então
    terminar) e False se o utilizador recusou ou ocorreu um erro.
    """
    if not IS_WINDOWS:
        log.warning("Reinício como administrador só é suportado no Windows.")
        return False
    try:
        import ctypes

        exe, params = _relaunch_command()
        workdir = os.path.dirname(exe) if getattr(sys, "frozen", False) else os.getcwd()
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
        ok = int(result) > 32
        if ok:
            log.info("Pedido de reinício como administrador aceite.")
        else:
            log.warning("Reinício como administrador recusado ou falhou (código %s).", result)
        return ok
    except Exception as exc:  # noqa: BLE001
        log.exception("Erro ao reiniciar como administrador: %s", exc)
        return False


@dataclass
class RestorePointResult:
    ok: bool
    message: str


_RESTORE_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
try {
    Checkpoint-Computer -Description '{desc}' -RestorePointType 'MODIFY_SETTINGS' -WarningVariable avisos -WarningAction SilentlyContinue
    if ($avisos) { Write-Output ('AVISO|' + ($avisos -join ' ')) } else { Write-Output 'OK|' }
} catch {
    Write-Output ('ERRO|' + $_.Exception.Message)
    exit 1
}
"""


def create_restore_point(description: str = "SafeOti - antes de alterações") -> RestorePointResult:
    """Cria um ponto de restauro com Checkpoint-Computer (PowerShell)."""
    if not IS_WINDOWS:
        return RestorePointResult(False, tr("Os pontos de restauro só existem no Windows."))
    if not is_admin():
        return RestorePointResult(
            False, tr("Criar um ponto de restauro exige privilégios de administrador. Reinicie o SafeOti como administrador.")
        )
    safe_desc = description.replace("'", "''")[:200]
    script = _RESTORE_SCRIPT.replace("{desc}", safe_desc)
    log.info("A criar ponto de restauro: %s", description)
    result = run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=300,
        encoding="utf-8",
    )
    text = result.stdout.strip()
    last = text.splitlines()[-1] if text else ""
    status, _, detail = last.partition("|")

    if result.ok and status == "OK":
        log.info("Ponto de restauro criado com sucesso.")
        return RestorePointResult(True, tr("Ponto de restauro criado com sucesso."))
    if result.ok and status == "AVISO":
        log.warning("Ponto de restauro não criado (aviso): %s", detail)
        lowered = detail.lower()
        if any(k in lowered for k in ("1440", "24 hour", "24 hora", "frequency", "frequência")):
            return RestorePointResult(
                False,
                tr("O Windows não criou um novo ponto de restauro porque já foi criado um nas últimas 24 horas. "
                "Pode continuar: o ponto de restauro recente continua disponível."),
            )
        return RestorePointResult(False, tr("O Windows não criou o ponto de restauro: {detail}", detail=detail))

    detail = detail or result.stderr.strip() or result.output or tr("erro desconhecido")
    log.error("Falha ao criar ponto de restauro: %s", detail)
    lowered = detail.lower()
    if any(k in lowered for k in ("disabled", "desativad", "desactivad", "0x80042306", "service cannot be started")):
        return RestorePointResult(
            False,
            tr("O Restauro do Sistema está desativado neste PC. Para o ativar, abra "
            "'Criar um ponto de restauro' no menu Iniciar, escolha a unidade C:, carregue em "
            "'Configurar' e ative a proteção do sistema."),
        )
    return RestorePointResult(
        False,
        tr("Não foi possível criar o ponto de restauro. Verifique se o Restauro do Sistema está ativo para a unidade do Windows.\n\nDetalhe: {detail}", detail=detail),
    )
