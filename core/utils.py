"""Funções auxiliares partilhadas pelos módulos de core/."""

from __future__ import annotations

import codecs
import locale
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .i18n import format_decimal, tr

log = logging.getLogger("safeoti.utils")

IS_WINDOWS = sys.platform == "win32"

# Impede que comandos de consola (powercfg, powershell, schtasks) abram janelas.
CREATE_NO_WINDOW = 0x08000000

ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], bool]


def no_progress(_percent: int, _message: str) -> None:
    """Callback de progresso que não faz nada."""


def never_cancel() -> bool:
    """Verificação de cancelamento que nunca cancela."""
    return False


class OperationCancelled(Exception):
    """A operação foi cancelada pelo utilizador."""


def format_bytes(num: float, decimals: int = 1) -> str:
    """Formata um número de bytes em unidades legíveis (B, KB, MB, GB, TB)."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0
    negative = value < 0
    value = abs(value)
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        text = f"{int(value)} {units[index]}"
    else:
        text = f"{format_decimal(value, decimals)} {units[index]}"
    return f"-{text}" if negative else text


def format_rate(bytes_per_second: float) -> str:
    """Formata uma taxa de transferência (bytes por segundo)."""
    return f"{format_bytes(bytes_per_second)}/s"


def _console_encoding() -> str:
    """Codificação usada pelas ferramentas de consola do Windows (página OEM)."""
    if IS_WINDOWS:
        try:
            codecs.lookup("oem")
            return "oem"
        except LookupError:
            pass
    return locale.getpreferredencoding(False) or "utf-8"


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout or "").strip() or (self.stderr or "").strip()


def run_hidden(
    args: Sequence[str],
    timeout: float = 60,
    encoding: Optional[str] = None,
) -> CommandResult:
    """Executa um comando sem abrir janela de consola e devolve o resultado.

    Nunca lança exceção por código de saída diferente de zero; erros de
    arranque (programa inexistente, tempo esgotado) são devolvidos como
    ``CommandResult`` com ``returncode`` negativo.
    """
    enc = encoding or _console_encoding()
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except FileNotFoundError:
        log.error("Comando não encontrado: %s", args[0] if args else "?")
        return CommandResult(-1, "", tr("Comando não encontrado: {command}", command=args[0] if args else "?"))
    except subprocess.TimeoutExpired:
        log.error("Tempo esgotado ao executar: %s", " ".join(args))
        return CommandResult(-2, "", tr("A operação demorou demasiado tempo e foi interrompida."))
    except OSError as exc:
        log.error("Erro ao executar %s: %s", " ".join(args), exc)
        return CommandResult(-3, "", str(exc))

    stdout = completed.stdout.decode(enc, errors="replace") if completed.stdout else ""
    stderr = completed.stderr.decode(enc, errors="replace") if completed.stderr else ""
    return CommandResult(completed.returncode, stdout, stderr)


def env_path(name: str, default: str = "") -> str:
    """Devolve o valor de uma variável de ambiente (caminho), ou ``default``."""
    value = os.environ.get(name, "").strip()
    return value or default
