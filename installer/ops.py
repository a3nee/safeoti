"""Operações de sistema do instalador: pastas, atalhos, registo do Windows.

Sem PySide6 — pode ser testado isoladamente. A instalação é por utilizador
(%LOCALAPPDATA%\\Programs\\SafeOti), por isso o instalador não precisa de
privilégios de administrador; só o SafeOti.exe os pede ao abrir.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "SafeOti"
EXE_NAME = "SafeOti.exe"
PUBLISHER = "SafeOti"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SafeOti"
UNINSTALL_FLAG = "--desinstalar"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

Progress = Callable[[float, str], None]


class InstallError(Exception):
    """Erro com mensagem pronta a mostrar ao utilizador."""


# ---------------------------------------------------------------- pastas

def _known_folder(csidl: int, fallback: str) -> str:
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:  # noqa: BLE001
            pass
    return fallback


def default_install_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return os.path.join(base, "Programs", APP_NAME)


def desktop_dir() -> str:
    return _known_folder(0x0010, str(Path.home() / "Desktop"))  # CSIDL_DESKTOPDIRECTORY


def start_menu_dir() -> str:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return _known_folder(0x0002, os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"))  # CSIDL_PROGRAMS


def desktop_shortcut() -> str:
    return os.path.join(desktop_dir(), f"{APP_NAME}.lnk")


def start_menu_shortcut() -> str:
    return os.path.join(start_menu_dir(), f"{APP_NAME}.lnk")


def data_dir() -> str:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return os.path.join(appdata, APP_NAME)


def normalize_target(folder: str) -> str:
    """Se o utilizador escolher p.ex. D:\\Apps, instala em D:\\Apps\\SafeOti."""
    folder = os.path.normpath(os.path.expandvars(folder.strip().strip('"')))
    if os.path.basename(folder).lower() != APP_NAME.lower():
        folder = os.path.join(folder, APP_NAME)
    return folder


def free_space(path: str) -> Optional[int]:
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free
    except OSError:
        return None


def check_writable(folder: str) -> None:
    try:
        os.makedirs(folder, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=folder, prefix=".safeoti_")
        os.close(fd)
        os.remove(probe)
    except PermissionError as exc:
        raise InstallError(
            "Não há permissão para escrever nesta pasta.\n"
            "Escolha uma pasta dentro do seu perfil (a pasta sugerida funciona sempre)."
        ) from exc
    except OSError as exc:
        raise InstallError(f"Não foi possível usar esta pasta: {exc.strerror or exc}") from exc


# ---------------------------------------------------------------- processos

def running_safeoti_pids(exclude: tuple[int, ...] = ()) -> list[int]:
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[0].lower() == EXE_NAME.lower():
            try:
                pid = int(row[1])
            except ValueError:
                continue
            if pid not in exclude:
                pids.append(pid)
    return pids


# ---------------------------------------------------------------- atalhos

_PS_SHORTCUT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SO_LNK);"
    "$s.TargetPath=$env:SO_TGT;$s.WorkingDirectory=$env:SO_WD;"
    "$s.IconLocation=$env:SO_TGT+',0';$s.Description='SafeOti - limpar, monitorizar e otimizar o PC';$s.Save()"
)


def create_shortcut(lnk: str, target: str) -> None:
    os.makedirs(os.path.dirname(lnk), exist_ok=True)
    env = dict(os.environ, SO_LNK=lnk, SO_TGT=target, SO_WD=os.path.dirname(target))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_SHORTCUT],
        capture_output=True, text=True, timeout=60, env=env, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0 or not os.path.exists(lnk):
        raise InstallError(f"Não foi possível criar o atalho {os.path.basename(lnk)}.")


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- registo (Adicionar/remover programas)

@dataclass
class InstalledInfo:
    location: str
    version: str


def read_installed() -> Optional[InstalledInfo]:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            location = winreg.QueryValueEx(key, "InstallLocation")[0]
            try:
                version = winreg.QueryValueEx(key, "DisplayVersion")[0]
            except OSError:
                version = ""
            return InstalledInfo(location, version)
    except OSError:
        return None


def write_uninstall_entry(folder: str, version: str, size_bytes: int) -> None:
    import winreg

    exe = os.path.join(folder, EXE_NAME)
    values = {
        "DisplayName": APP_NAME,
        "DisplayVersion": version,
        "Publisher": PUBLISHER,
        "DisplayIcon": f"{exe},0",
        "InstallLocation": folder,
        "UninstallString": f'"{exe}" {UNINSTALL_FLAG}',
        "QuietUninstallString": f'"{exe}" {UNINSTALL_FLAG}',
    }
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_WRITE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, max(1, size_bytes // 1024))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def delete_uninstall_entry() -> None:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- instalar

def copy_with_progress(src: str, dst: str, progress: Progress, chunk: int = 1 << 20) -> None:
    total = max(1, os.path.getsize(src))
    tmp = dst + ".novo"
    done = 0
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        while True:
            block = fin.read(chunk)
            if not block:
                break
            fout.write(block)
            done += len(block)
            progress(done / total, "A copiar ficheiros…")
    try:
        os.replace(tmp, dst)
    except PermissionError as exc:
        remove_file(tmp)
        raise InstallError("O SafeOti está aberto. Feche-o e tente de novo.") from exc


def install(payload: str, folder: str, version: str, desktop: bool, start_menu: bool, progress: Progress) -> str:
    """Instala e devolve o caminho do SafeOti.exe instalado. Progresso de 0 a 1."""
    if running_safeoti_pids():
        raise InstallError("O SafeOti está aberto. Feche-o (também na área de notificação) e tente de novo.")
    if not os.path.isfile(payload):
        raise InstallError("O instalador está danificado: não encontrei o SafeOti.exe lá dentro.")
    progress(0.0, "A preparar a pasta…")
    check_writable(folder)
    exe = os.path.join(folder, EXE_NAME)
    copy_with_progress(payload, exe, lambda f, m: progress(0.02 + f * 0.83, m))

    progress(0.86, "A criar atalhos…")
    for wanted, lnk in ((start_menu, start_menu_shortcut()), (desktop, desktop_shortcut())):
        if wanted:
            create_shortcut(lnk, exe)
        else:
            remove_file(lnk)

    progress(0.95, "A registar no Windows…")
    write_uninstall_entry(folder, version, os.path.getsize(exe))
    progress(1.0, "Concluído")
    return exe


# ---------------------------------------------------------------- desinstalar

def uninstall(folder: str, remove_data: bool, progress: Progress) -> None:
    """Remove atalhos, registo, arranque automático e (opcional) dados. A pasta
    da aplicação é apagada depois de o processo terminar (schedule_folder_removal)."""
    progress(0.05, "A desativar o arranque automático…")
    try:
        from core.startup import set_self_autostart

        set_self_autostart(False)
    except Exception:  # noqa: BLE001 — sem tarefa ou sem permissões: não impede a desinstalação
        pass

    progress(0.35, "A remover atalhos…")
    remove_file(desktop_shortcut())
    remove_file(start_menu_shortcut())

    progress(0.6, "A remover do registo do Windows…")
    delete_uninstall_entry()

    if remove_data:
        progress(0.8, "A apagar definições e registos…")
        shutil.rmtree(data_dir(), ignore_errors=True)
    progress(1.0, "Concluído")


def schedule_folder_removal(folder: str) -> None:
    """Apaga SafeOti.exe e a pasta (só se ficar vazia) depois de este processo sair.

    Nunca apaga outros ficheiros: o rmdir sem /s falha se a pasta tiver mais coisas.
    """
    # Os caminhos passam por variáveis de ambiente: o cmd expande-as em Unicode,
    # por isso pastas com acentos (C:\Users\António) funcionam sem problemas.
    env = dict(os.environ, SO_EXE=os.path.join(folder, EXE_NAME), SO_DIR=folder)
    line = (
        'for /l %i in (1,1,30) do @(if exist "%SO_EXE%" '
        '(del /f /q "%SO_EXE%" >nul 2>nul & ping -n 2 127.0.0.1 >nul)) '
        '& rmdir "%SO_DIR%" >nul 2>nul'
    )
    subprocess.Popen(
        f'cmd.exe /d /s /c "{line}"',
        env=env,
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
        cwd=tempfile.gettempdir(),
    )
