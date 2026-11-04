"""Programas instalados: listagem (registo + Microsoft Store) e desinstalação.

Lê as chaves ``Uninstall`` do registo (HKLM 64 bits, HKLM 32 bits/WOW6432Node e
HKCU), esconde componentes do sistema e atualizações, e remove duplicados.
A desinstalação abre sempre o desinstalador interativo do programa (nunca o
``QuietUninstallString`` em silêncio): para MSI usa ``msiexec /x {GUID}``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import admin
from .i18n import tr
from .utils import IS_WINDOWS, CancelCheck, ProgressCallback, never_cancel, no_progress, run_hidden

try:
    import winreg
except ImportError:  # pragma: no cover - fora do Windows
    winreg = None  # type: ignore[assignment]

log = logging.getLogger("safeoti.programs")

UNINSTALL_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_PATH_WOW = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
APPX_REPOSITORY = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
                   r"\AppModel\Repository\Packages")

SELF_NAME = "safeoti"
_HIDDEN_RELEASE_TYPES = {"update", "hotfix", "security update", "service pack", "update rollup"}
_KB_RE = re.compile(r"\bKB\d{6,8}\b", re.IGNORECASE)
_GUID_RE = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")
_MSIEXEC_RE = re.compile(r"msiexec(\.exe)?", re.IGNORECASE)

# Códigos de saída habituais dos instaladores (msiexec e outros).
EXIT_USER_CANCELLED = 1602
EXIT_REBOOT_REQUIRED = (3010, 1641)
_ERROR_ELEVATION_REQUIRED = 740


class ProgramsError(Exception):
    """Erro com mensagem já pronta para mostrar ao utilizador."""


class ProgramsPermissionError(ProgramsError):
    """A operação exige privilégios de administrador."""


class SelfUninstallError(ProgramsError):
    """Tentativa de desinstalar o próprio SafeOti sem confirmação explícita."""


@dataclass
class Program:
    name: str
    version: str = ""
    publisher: str = ""
    install_date: Optional[date] = None
    size_bytes: int = 0  # 0 = desconhecido
    install_location: str = ""
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    display_icon: str = ""
    windows_installer: bool = False
    key_name: str = ""
    key_path: str = ""  # caminho completo, ex.: HKLM\Software\...\Uninstall\{GUID}
    hive: str = ""  # "HKLM", "HKCU" ou "" (Microsoft Store)
    kind: str = "registry"  # "registry" ou "store"
    package_full_name: str = ""

    @property
    def is_store(self) -> bool:
        return self.kind == "store"

    @property
    def requires_admin(self) -> bool:
        return self.hive == "HKLM"

    @property
    def is_msi(self) -> bool:
        return self.windows_installer or bool(_MSIEXEC_RE.search(self.uninstall_string or ""))

    @property
    def is_self(self) -> bool:
        return SELF_NAME in (self.name or "").lower()

    @property
    def product_code(self) -> str:
        """GUID do produto MSI (do nome da chave ou da linha de desinstalação)."""
        if _GUID_RE.fullmatch(self.key_name or ""):
            return self.key_name.upper()
        match = _GUID_RE.search(self.uninstall_string or "")
        return match.group(0).upper() if match else ""

    @property
    def uid(self) -> str:
        return self.package_full_name if self.is_store else self.key_path or self.name


@dataclass
class UninstallResult:
    name: str
    returncode: int = 0
    launched_only: bool = False  # lançado sem esperar (ex.: o próprio SafeOti)
    stopped_waiting: bool = False  # a app fechou antes de o desinstalador terminar

    @property
    def cancelled(self) -> bool:
        return self.returncode == EXIT_USER_CANCELLED

    @property
    def reboot_required(self) -> bool:
        return self.returncode in EXIT_REBOOT_REQUIRED

    @property
    def ok(self) -> bool:
        return self.returncode == 0 or self.reboot_required


# --------------------------------------------------------------------------- #
# Lógica pura (testável sem registo)
# --------------------------------------------------------------------------- #
def parse_install_date(value: object) -> Optional[date]:
    """Converte ``InstallDate`` (normalmente AAAAMMDD) numa data; None se inválida."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text) or re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
        if not match:
            return None
        first, second, year = (int(g) for g in match.groups())
        # Ambíguo: os instaladores costumam usar o formato dos EUA (M/D/AAAA).
        month, day = (first, second) if first <= 12 else (second, first)
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if parsed.year < 1980 or parsed.year > date.today().year + 1:
        return None
    return parsed


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            return int(str(value).strip(), 0)
        except (TypeError, ValueError):
            return 0


def _as_str(value: object) -> str:
    return str(value).strip() if value is not None and not isinstance(value, (bytes, bytearray)) else ""


def is_hidden_entry(values: Dict[str, object], key_name: str = "") -> bool:
    """Indica se uma entrada Uninstall deve ser escondida (sistema, atualizações, incompleta)."""
    name = _as_str(values.get("DisplayName"))
    if not name or not _as_str(values.get("UninstallString")):
        return True
    if _as_int(values.get("SystemComponent")) == 1:
        return True
    if _as_str(values.get("ParentKeyName")):
        return True
    if _as_str(values.get("ReleaseType")).lower() in _HIDDEN_RELEASE_TYPES:
        return True
    if re.fullmatch(r"KB\d+(v\d+)?", key_name or "", re.IGNORECASE) or _KB_RE.search(name):
        return True
    return False


def program_from_values(values: Dict[str, object], hive: str, key_path: str, key_name: str) -> Optional[Program]:
    """Constrói um Program a partir dos valores de uma chave Uninstall (None se escondido)."""
    if is_hidden_entry(values, key_name):
        return None
    size_kb = _as_int(values.get("EstimatedSize"))
    return Program(
        name=_as_str(values.get("DisplayName")),
        version=_as_str(values.get("DisplayVersion")),
        publisher=_as_str(values.get("Publisher")),
        install_date=parse_install_date(values.get("InstallDate")),
        size_bytes=size_kb * 1024 if size_kb > 0 else 0,
        install_location=_as_str(values.get("InstallLocation")).strip('"'),
        uninstall_string=_as_str(values.get("UninstallString")),
        quiet_uninstall_string=_as_str(values.get("QuietUninstallString")),
        display_icon=_as_str(values.get("DisplayIcon")),
        windows_installer=_as_int(values.get("WindowsInstaller")) == 1,
        key_name=key_name,
        key_path=key_path,
        hive=hive,
    )


def _richness(program: Program) -> int:
    fields = (program.publisher, program.install_date, program.size_bytes, program.install_location,
              program.display_icon, program.version)
    return sum(1 for f in fields if f)


def dedupe(programs: Iterable[Program]) -> List[Program]:
    """Remove entradas com o mesmo nome e versão (ex.: registadas em várias vistas do registo).

    Fica a entrada com mais informação; em empate, a primeira encontrada.
    """
    best: Dict[Tuple[str, str], Program] = {}
    order: List[Tuple[str, str]] = []
    for program in programs:
        key = (" ".join(program.name.lower().split()), program.version.strip().lower())
        current = best.get(key)
        if current is None:
            best[key] = program
            order.append(key)
        elif _richness(program) > _richness(current):
            best[key] = program
    return [best[k] for k in order]


def split_command(command: str, exists: Callable[[str], bool] = os.path.isfile) -> Tuple[str, str]:
    """Separa uma linha de comando em (executável, argumentos).

    Trata caminhos entre aspas, caminhos sem aspas com espaços
    (``C:\\Program Files\\App\\uninst.exe /S``) e formas ``rundll32``.
    Os argumentos são devolvidos tal como estão, para serem passados ao
    programa sem alterações.
    """
    text = (command or "").strip()
    if not text:
        return "", ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1:
            return text[1:].strip(), ""
        return text[1:end].strip(), text[end + 1:].strip()
    # Sem aspas: procura o prefixo mais longo que seja um ficheiro existente.
    parts = text.split(" ")
    for count in range(len(parts), 0, -1):
        candidate = " ".join(parts[:count]).rstrip()
        if not candidate:
            continue
        for path in (candidate, candidate + ".exe"):
            if ("\\" in path or "/" in path) and exists(path):
                return path, " ".join(parts[count:]).strip()
    # Ficheiro inexistente (ou testes): corta a seguir ao primeiro ".exe".
    match = re.search(r"\.exe(?=\s|$)", text, re.IGNORECASE)
    if match:
        return text[:match.end()].strip(), text[match.end():].strip()
    first, _, rest = text.partition(" ")
    return first, rest.strip()


def _quote(path: str) -> str:
    return f'"{path}"' if " " in path and not path.startswith('"') else path


def uninstall_command_line(program: Program, exists: Callable[[str], bool] = os.path.isfile) -> str:
    """Linha de comando interativa para desinstalar um programa do registo."""
    if program.is_msi:
        code = program.product_code
        if code:
            return f"msiexec.exe /x {code}"
    raw = os.path.expandvars(program.uninstall_string)
    exe, args = split_command(raw, exists)
    if not exe:
        raise ProgramsError(tr("'{name}' não indica como deve ser desinstalado.", name=program.name))
    return f"{_quote(exe)} {args}".strip()


def location_candidates(program: Program) -> List[str]:
    """Pastas possíveis do programa, por ordem de preferência (sem verificar o disco)."""
    result: List[str] = []
    if program.install_location:
        result.append(os.path.expandvars(program.install_location))
    if program.display_icon:
        icon = os.path.expandvars(program.display_icon).strip()
        icon = re.sub(r",\s*-?\d+$", "", icon).strip().strip('"')
        if icon:
            result.append(os.path.dirname(icon))
    if program.uninstall_string and not program.is_msi:
        exe, _ = split_command(os.path.expandvars(program.uninstall_string), lambda _p: False)
        if exe and ("\\" in exe or "/" in exe) and "rundll32" not in exe.lower():
            result.append(os.path.dirname(exe))
    return [p for p in result if p]


def program_location(program: Program) -> str:
    """Primeira pasta existente do programa, ou ""."""
    for path in location_candidates(program):
        if os.path.isdir(path):
            return os.path.normpath(path)
    return ""


def publisher_from_dn(dn: str) -> str:
    """Extrai o nome do editor de um "distinguished name" (``CN=..., O=Empresa``).

    Prefere ``O=`` porque nas aplicações da Store o ``CN`` é muitas vezes um GUID.
    """
    for field in ("O", "CN"):
        match = re.search(rf"(?:^|,\s*){field}=(\"[^\"]+\"|[^,]+)", dn or "")
        if match:
            value = match.group(1).strip().strip('"')
            # Editores registados só com um identificador (GUID) não dizem nada ao utilizador.
            return "" if _GUID_RE.fullmatch("{" + value + "}") else value
    return (dn or "").strip()


def parse_appx_json(text: str) -> List[Program]:
    """Converte a saída JSON de Get-AppxPackage em Programs (lista ou objeto único)."""
    text = (text or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    result: List[Program] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        full = _as_str(item.get("PackageFullName"))
        if not full:
            continue
        result.append(Program(
            name=_as_str(item.get("DisplayName")) or _as_str(item.get("Name")) or full,
            version=_as_str(item.get("Version")),
            publisher=publisher_from_dn(_as_str(item.get("Publisher"))),
            install_location=_as_str(item.get("InstallLocation")),
            key_name=_as_str(item.get("Name")),
            kind="store",
            package_full_name=full,
        ))
    return result


# --------------------------------------------------------------------------- #
# Registo
# --------------------------------------------------------------------------- #
def _read_values(key) -> Dict[str, object]:
    values: Dict[str, object] = {}
    index = 0
    while True:
        try:
            name, data, _vtype = winreg.EnumValue(key, index)
        except OSError:
            break
        values[name] = data
        index += 1
    return values


def _registry_sources():
    return [
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, UNINSTALL_PATH, winreg.KEY_WOW64_64KEY, UNINSTALL_PATH),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, UNINSTALL_PATH, winreg.KEY_WOW64_32KEY, UNINSTALL_PATH_WOW),
        ("HKCU", winreg.HKEY_CURRENT_USER, UNINSTALL_PATH, 0, UNINSTALL_PATH),
    ]


def list_registry_programs() -> List[Program]:
    """Programas das chaves Uninstall (sem duplicados nem atualizações)."""
    if not IS_WINDOWS or winreg is None:
        return []
    found: List[Program] = []
    for hive_name, hive, path, view, shown_path in _registry_sources():
        try:
            root = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except OSError as exc:
            log.warning("Não foi possível abrir %s\\%s: %s", hive_name, shown_path, exc)
            continue
        with root:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        values = _read_values(sub)
                except OSError:
                    continue
                program = program_from_values(values, hive_name, f"{hive_name}\\{shown_path}\\{sub_name}", sub_name)
                if program is not None:
                    found.append(program)
    return dedupe(found)


_APPX_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$items = @(Get-AppxPackage | Where-Object {
    -not $_.IsFramework -and -not $_.IsResourcePackage -and -not $_.NonRemovable -and
    $_.SignatureKind.ToString() -eq 'Store'
} | Select-Object Name, PackageFullName, Version, Publisher, InstallLocation)
if ($items.Count -gt 0) { ConvertTo-Json -InputObject $items -Compress -Depth 2 }
"""


def _resolve_indirect(text: str) -> str:
    """Resolve "@{Pacote?ms-resource:...}" com SHLoadIndirectString (ou devolve "")."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        hr = ctypes.windll.shlwapi.SHLoadIndirectString(text, buffer, len(buffer), None)
        return buffer.value.strip() if hr == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def resource_candidates(value: str, package_full_name: str, package_name: str) -> List[str]:
    """Referências indiretas a tentar para um nome "ms-resource:..." de um pacote."""
    if value.startswith("@"):
        return [value]
    if not value.lower().startswith("ms-resource:"):
        return []
    rest = value[len("ms-resource:"):]
    if rest.startswith("//"):
        return [f"@{{{package_full_name}?{value}}}"]
    rest = rest.lstrip("/")
    uris = [f"ms-resource://{package_name}/{rest}"]
    if not rest.lower().startswith("resources/"):
        uris.insert(0, f"ms-resource://{package_name}/resources/{rest}")
    return [f"@{{{package_full_name}?{uri}}}" for uri in uris]


def _store_display_name(package_full_name: str, package_name: str) -> str:
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{APPX_REPOSITORY}\\{package_full_name}") as key:
            value, _ = winreg.QueryValueEx(key, "DisplayName")
    except OSError:
        return ""
    value = _as_str(value)
    candidates = resource_candidates(value, package_full_name, package_name)
    if not candidates:
        return value
    for candidate in candidates:
        resolved = _resolve_indirect(candidate)
        if resolved and not resolved.lower().startswith(("ms-resource:", "@{")):
            return resolved
    return ""


def list_store_apps() -> List[Program]:
    """Aplicações da Microsoft Store do utilizador. Devolve [] se algo falhar."""
    if not IS_WINDOWS:
        return []
    result = run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _APPX_SCRIPT],
        timeout=60, encoding="utf-8",
    )
    if not result.ok:
        log.warning("Get-AppxPackage falhou: %s", result.output)
        return []
    try:
        apps = parse_appx_json(result.stdout)
    except (ValueError, TypeError) as exc:
        log.warning("Saída inválida de Get-AppxPackage: %s", exc)
        return []
    for app in apps:
        friendly = _store_display_name(app.package_full_name, app.key_name)
        if friendly:
            app.name = friendly
    return apps


def list_programs(include_store: bool = True) -> List[Program]:
    programs = list_registry_programs()
    if include_store:
        try:
            programs.extend(list_store_apps())
        except Exception:  # noqa: BLE001 - a Store é opcional
            log.exception("Erro ao listar as aplicações da Microsoft Store")
    programs.sort(key=lambda p: p.name.lower())
    return programs


# --------------------------------------------------------------------------- #
# Desinstalação
# --------------------------------------------------------------------------- #
def _remove_store_app(program: Program) -> UninstallResult:
    full = program.package_full_name.replace("'", "''")
    result = run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         f"$ErrorActionPreference = 'Stop'; Remove-AppxPackage -Package '{full}'"],
        timeout=600, encoding="utf-8",
    )
    if not result.ok:
        log.error("Remove-AppxPackage falhou para %s: %s", program.package_full_name, result.output)
        raise ProgramsError(tr("Não foi possível remover a aplicação '{name}': {detail}",
                               name=program.name, detail=result.output or result.returncode))
    log.info("Aplicação da Store removida: %s", program.package_full_name)
    return UninstallResult(program.name, 0)


def uninstall(
    program: Program,
    allow_self: bool = False,
    wait: bool = True,
    progress: ProgressCallback = no_progress,
    should_cancel: CancelCheck = never_cancel,
) -> UninstallResult:
    """Abre o desinstalador interativo do programa e (opcionalmente) espera que termine.

    ``should_cancel`` só deixa de esperar: o desinstalador continua aberto.
    """
    if program.is_self and not allow_self:
        raise SelfUninstallError(tr("Desinstalar o SafeOti fecha a aplicação. Confirme primeiro."))
    if program.is_store:
        progress(0, tr("A remover '{name}'…", name=program.name))
        return _remove_store_app(program)
    if program.requires_admin and not admin.is_admin():
        raise ProgramsPermissionError(
            tr("'{name}' foi instalado para todos os utilizadores: desinstalar exige privilégios de administrador.",
               name=program.name))
    command = uninstall_command_line(program)
    log.info("A desinstalar %s: %s", program.name, command)
    progress(0, tr("A aguardar o desinstalador de '{name}'…", name=program.name))
    try:
        proc = subprocess.Popen(command, close_fds=True)  # noqa: S603 - linha do próprio programa
    except FileNotFoundError:
        raise ProgramsError(tr("O desinstalador de '{name}' não foi encontrado. O programa pode já ter sido removido.",
                               name=program.name)) from None
    except OSError as exc:
        if getattr(exc, "winerror", None) == _ERROR_ELEVATION_REQUIRED:
            raise ProgramsPermissionError(
                tr("O desinstalador de '{name}' exige privilégios de administrador.", name=program.name)) from None
        raise ProgramsError(tr("Não foi possível abrir o desinstalador de '{name}': {detail}",
                               name=program.name, detail=exc.strerror or exc)) from None
    if not wait:
        return UninstallResult(program.name, 0, launched_only=True)
    while proc.poll() is None:
        if should_cancel():
            return UninstallResult(program.name, 0, stopped_waiting=True)
        time.sleep(0.5)
    log.info("Desinstalador de %s terminou com o código %s", program.name, proc.returncode)
    return UninstallResult(program.name, proc.returncode)
