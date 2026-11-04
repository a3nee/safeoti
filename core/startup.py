"""Gestor de arranque: lista e ativa/desativa programas que iniciam com o Windows.

A ativação/desativação usa as mesmas chaves que o Gestor de Tarefas do Windows
(``Explorer\\StartupApproved``). As entradas originais nunca são apagadas, por
isso qualquer alteração é reversível (aqui ou no Gestor de Tarefas).

Também contém o registo do próprio SafeOti para iniciar com o Windows,
através de uma tarefa agendada (necessária porque a app pede privilégios de
administrador e o Windows não inicia programas elevados a partir da chave Run).
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from xml.sax.saxutils import escape

from .i18n import tr
from .utils import IS_WINDOWS, env_path, run_hidden

if IS_WINDOWS:
    import winreg
else:  # permite importar o módulo (e testar a lógica) fora do Windows
    winreg = None  # type: ignore[assignment]

log = logging.getLogger("safeoti.startup")

RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_PATH_WOW = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
APPROVED_BASE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

SELF_TASK_NAME = "SafeOti"


class StartupPermissionError(Exception):
    """A alteração exige privilégios de administrador."""


class StartupError(Exception):
    """Erro genérico ao alterar uma entrada de arranque."""


@dataclass(frozen=True)
class StartupSource:
    id: str
    label: str
    hive_name: str  # "HKCU" ou "HKLM"
    kind: str  # "registry" ou "folder"
    location: str  # caminho da chave Run ou da pasta de arranque
    approved_subkey: str  # subchave em StartupApproved (Run, Run32, StartupFolder)

    @property
    def requires_admin(self) -> bool:
        return self.hive_name == "HKLM"


@dataclass
class StartupEntry:
    name: str  # nome do valor no registo ou nome do ficheiro na pasta
    command: str
    source: StartupSource
    enabled: bool

    @property
    def requires_admin(self) -> bool:
        return self.source.requires_admin

    @property
    def display_name(self) -> str:
        if self.source.kind == "folder":
            base, ext = os.path.splitext(self.name)
            return base if ext.lower() in (".lnk", ".url") else self.name
        return self.name

    @property
    def key(self) -> Tuple[str, str]:
        return self.source.id, self.name


def _user_startup_folder() -> str:
    appdata = env_path("APPDATA")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup") if appdata else ""


def _common_startup_folder() -> str:
    programdata = env_path("ProgramData", "C:\\ProgramData")
    return os.path.join(programdata, "Microsoft", "Windows", "Start Menu", "Programs", "StartUp")


def get_sources() -> List[StartupSource]:
    return [
        StartupSource("hkcu_run", tr("Registo (utilizador)"), "HKCU", "registry", RUN_PATH, "Run"),
        StartupSource("hkcu_run32", tr("Registo (utilizador, 32 bits)"), "HKCU", "registry", RUN_PATH_WOW, "Run32"),
        StartupSource("hklm_run", tr("Registo (todos os utilizadores)"), "HKLM", "registry", RUN_PATH, "Run"),
        StartupSource("hklm_run32", tr("Registo (todos os utilizadores, 32 bits)"), "HKLM", "registry", RUN_PATH_WOW, "Run32"),
        StartupSource("user_folder", tr("Pasta de arranque (utilizador)"), "HKCU", "folder", _user_startup_folder(), "StartupFolder"),
        StartupSource("common_folder", tr("Pasta de arranque (todos os utilizadores)"), "HKLM", "folder", _common_startup_folder(), "StartupFolder"),
    ]


# --------------------------------------------------------------------------- #
# Formato StartupApproved
# --------------------------------------------------------------------------- #
# Valor REG_BINARY de 12 bytes. O primeiro byte indica o estado:
#   0x02 / 0x06 (par)  -> ativo
#   0x03 / 0x07 (ímpar) -> desativado
# Os bytes 4..11 guardam a data (FILETIME) em que foi desativado.
APPROVED_ENABLED = bytes([0x02, 0, 0, 0]) + bytes(8)


def parse_approved(data: Optional[bytes]) -> bool:
    """Devolve True (ativo) se o valor não existir ou tiver o primeiro byte par."""
    if not data:
        return True
    return (data[0] & 0x01) == 0


def approved_disabled_value(now: Optional[float] = None) -> bytes:
    """Valor binário de 'desativado', com a data atual em formato FILETIME."""
    timestamp = time.time() if now is None else now
    filetime = int((timestamp + 11644473600) * 10_000_000)
    return bytes([0x03, 0, 0, 0]) + struct.pack("<Q", filetime)


# --------------------------------------------------------------------------- #
# Acesso ao registo
# --------------------------------------------------------------------------- #
def _hive(name: str):
    return winreg.HKEY_CURRENT_USER if name == "HKCU" else winreg.HKEY_LOCAL_MACHINE


def _read_values(hive_name: str, path: str) -> List[Tuple[str, object, int]]:
    values: List[Tuple[str, object, int]] = []
    try:
        with winreg.OpenKey(_hive(hive_name), path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            index = 0
            while True:
                try:
                    values.append(winreg.EnumValue(key, index))
                except OSError:
                    break
                index += 1
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Não foi possível ler %s\\%s: %s", hive_name, path, exc)
    return values


def _read_approved(source: StartupSource) -> dict:
    result = {}
    for name, data, vtype in _read_values(source.hive_name, f"{APPROVED_BASE}\\{source.approved_subkey}"):
        if vtype == winreg.REG_BINARY and isinstance(data, (bytes, bytearray)):
            result[name.lower()] = bytes(data)
    return result


def list_entries() -> List[StartupEntry]:
    """Lista todas as entradas de arranque das fontes suportadas."""
    if not IS_WINDOWS:
        return []
    entries: List[StartupEntry] = []
    for source in get_sources():
        approved = _read_approved(source)
        if source.kind == "registry":
            for name, data, vtype in _read_values(source.hive_name, source.location):
                if not name or vtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    continue
                command = str(data)
                if vtype == winreg.REG_EXPAND_SZ:
                    command = os.path.expandvars(command)
                entries.append(StartupEntry(name, command, source, parse_approved(approved.get(name.lower()))))
        else:
            folder = source.location
            if not folder or not os.path.isdir(folder):
                continue
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        if entry.name.lower() == "desktop.ini" or not entry.is_file(follow_symlinks=False):
                            continue
                        entries.append(
                            StartupEntry(entry.name, entry.path, source, parse_approved(approved.get(entry.name.lower())))
                        )
            except OSError as exc:
                log.warning("Não foi possível ler a pasta de arranque %s: %s", folder, exc)
    entries.sort(key=lambda e: e.display_name.lower())
    return entries


def set_enabled(entry: StartupEntry, enabled: bool) -> None:
    """Ativa ou desativa uma entrada escrevendo em StartupApproved (nunca apaga a entrada)."""
    if not IS_WINDOWS:
        raise StartupError(tr("Só disponível no Windows."))
    path = f"{APPROVED_BASE}\\{entry.source.approved_subkey}"
    value = APPROVED_ENABLED if enabled else approved_disabled_value()
    try:
        with winreg.CreateKeyEx(
            _hive(entry.source.hive_name), path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
        ) as key:
            winreg.SetValueEx(key, entry.name, 0, winreg.REG_BINARY, value)
    except PermissionError as exc:
        log.warning("Sem permissão para alterar '%s' (%s): %s", entry.name, entry.source.label, exc)
        raise StartupPermissionError(
            tr("Alterar '{name}' exige privilégios de administrador, porque se aplica a todos os utilizadores.", name=entry.display_name)
        ) from exc
    except OSError as exc:
        log.error("Erro ao alterar '%s': %s", entry.name, exc)
        raise StartupError(tr("Não foi possível alterar '{name}': {error}", name=entry.display_name, error=exc)) from exc
    entry.enabled = enabled
    log.info(
        "Arranque: '%s' (%s) %s", entry.name, entry.source.label, "ativado" if enabled else "desativado"
    )


def entry_location(entry: StartupEntry) -> Optional[str]:
    """Pasta a abrir no Explorador para esta entrada (se for possível determiná-la)."""
    if entry.source.kind == "folder":
        return entry.command
    exe = extract_executable(entry.command)
    return exe if exe and os.path.exists(exe) else None


def extract_executable(command: str) -> Optional[str]:
    """Extrai o caminho do executável de uma linha de comando."""
    cmd = command.strip()
    if not cmd:
        return None
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 1 else cmd.strip('"')
    lowered = cmd.lower()
    for ext in (".exe", ".bat", ".cmd", ".com"):
        pos = lowered.find(ext)
        if pos != -1:
            return cmd[: pos + len(ext)]
    return cmd.split(" ")[0]


# --------------------------------------------------------------------------- #
# Iniciar o próprio SafeOti com o Windows (tarefa agendada)
# --------------------------------------------------------------------------- #
def _self_command() -> Tuple[str, str, str]:
    """(executável, argumentos, pasta de trabalho) para lançar o SafeOti."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, "--minimizado", os.path.dirname(exe)
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.exists(pythonw):
        python = pythonw
    script = os.path.abspath(sys.argv[0])
    return python, f'"{script}" --minimizado', os.path.dirname(script)


def _task_xml() -> str:
    exe, args, workdir = _self_command()
    domain = env_path("USERDOMAIN")
    user = env_path("USERNAME")
    user_id = f"{domain}\\{user}" if domain else user
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Inicia o SafeOti ao iniciar sessão.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user_id)}</UserId>
      <Delay>PT15S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def is_self_autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    return run_hidden(["schtasks.exe", "/Query", "/TN", SELF_TASK_NAME], timeout=20).ok


def set_self_autostart(enabled: bool) -> None:
    """Cria ou remove a tarefa agendada que inicia o SafeOti com o Windows."""
    if not IS_WINDOWS:
        raise StartupError(tr("Só disponível no Windows."))
    from .admin import is_admin

    if not is_admin():
        raise StartupPermissionError(
            tr("Para iniciar o SafeOti com o Windows é preciso criar uma tarefa agendada, o que exige privilégios de administrador.")
        )
    if enabled:
        fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="safeoti_")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as fh:
                fh.write(_task_xml())
            result = run_hidden(
                ["schtasks.exe", "/Create", "/TN", SELF_TASK_NAME, "/XML", xml_path, "/F"], timeout=30
            )
        finally:
            try:
                os.remove(xml_path)
            except OSError:
                pass
        if not result.ok:
            log.error("Falha ao criar a tarefa de arranque: %s", result.output)
            raise StartupError(tr("Não foi possível configurar o arranque automático: {detail}", detail=result.output))
        log.info("Arranque automático do SafeOti ativado (tarefa agendada).")
    else:
        if not is_self_autostart_enabled():
            return
        result = run_hidden(["schtasks.exe", "/Delete", "/TN", SELF_TASK_NAME, "/F"], timeout=30)
        if not result.ok:
            log.error("Falha ao remover a tarefa de arranque: %s", result.output)
            raise StartupError(tr("Não foi possível remover o arranque automático: {detail}", detail=result.output))
        log.info("Arranque automático do SafeOti desativado.")
