"""Análise do espaço em disco: unidades, árvore de pastas e maiores ficheiros.

Regras:
* a análise é iterativa (sem recursão) e nunca segue symlinks, junctions nem
  outros reparse points (exceto pastas da nuvem, como as do OneDrive, que são
  pastas reais com um reparse point de sincronização);
* só as pastas ficam na árvore: os ficheiros contam para os tamanhos e apenas os
  maiores são guardados (num heap), para uma análise de C:\\ caber em memória;
* entradas inacessíveis são ignoradas e contadas;
* "Enviar para a Reciclagem" usa a API do Windows com FOF_ALLOWUNDO (nunca apaga
  de forma permanente sem o aviso do próprio Windows) e recusa locais protegidos.
"""

from __future__ import annotations

import heapq
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import psutil

from .cleaner import _forbidden_roots, _norm
from .i18n import tr
from .utils import (
    IS_WINDOWS,
    CancelCheck,
    OperationCancelled,
    ProgressCallback,
    env_path,
    format_bytes,
    never_cancel,
    no_progress,
)

log = logging.getLogger("safeoti.diskspace")

FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_NOT_LOCAL = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_LINK_TAGS = {IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_APPEXECLINK}

DEFAULT_TOP_FILES = 100
PROGRESS_INTERVAL = 0.2


class DiskSpaceError(OSError):
    """Erro com mensagem pronta a mostrar (já traduzida)."""


# --------------------------------------------------------------------------- #
# Unidades
# --------------------------------------------------------------------------- #
@dataclass
class DriveInfo:
    mountpoint: str
    label: str
    fstype: str
    total: int
    used: int
    free: int

    @property
    def percent(self) -> float:
        return (self.used * 100.0 / self.total) if self.total else 0.0


def _volume_label(mountpoint: str) -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(mountpoint), buf, len(buf), None, None, None, None, 0
        )
        return buf.value if ok else ""
    except Exception:  # noqa: BLE001
        return ""


def list_drives() -> List[DriveInfo]:
    """Unidades fixas (discos locais) com espaço total, usado e livre."""
    drives: List[DriveInfo] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:  # noqa: BLE001
        log.exception("Não foi possível listar as unidades")
        return drives
    seen = set()
    for part in partitions:
        opts = (part.opts or "").lower()
        if IS_WINDOWS and ("cdrom" in opts or "fixed" not in opts):
            continue
        if part.mountpoint in seen:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except Exception:  # noqa: BLE001 - unidade não pronta, sem permissão…
            continue
        seen.add(part.mountpoint)
        drives.append(DriveInfo(part.mountpoint, _volume_label(part.mountpoint), part.fstype or "",
                                int(usage.total), int(usage.used), int(usage.free)))
    drives.sort(key=lambda d: d.mountpoint.lower())
    return drives


# --------------------------------------------------------------------------- #
# Árvore
# --------------------------------------------------------------------------- #
class DirNode:
    """Pasta da árvore. ``size``/``file_count`` incluem todas as subpastas."""

    __slots__ = ("name", "parent", "children", "size", "file_count", "own_size", "own_files",
                 "inaccessible", "_path")
    is_dir = True

    def __init__(self, name: str, parent: Optional["DirNode"] = None, path: str = "") -> None:
        self.name = name
        self.parent = parent
        self.children: List[DirNode] = []
        self.size = 0          # bytes (total, com subpastas)
        self.file_count = 0    # ficheiros (total, com subpastas)
        self.own_size = 0      # bytes dos ficheiros diretamente nesta pasta
        self.own_files = 0
        self.inaccessible = False
        self._path = path      # só guardado na raiz; o resto é calculado

    @property
    def path(self) -> str:
        if self.parent is None:
            return self._path
        parts = []
        node: Optional[DirNode] = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return os.path.join(node._path if node else "", *reversed(parts))

    def sorted_children(self) -> List["DirNode"]:
        return sorted(self.children, key=lambda n: n.size, reverse=True)

    def __repr__(self) -> str:  # pragma: no cover - ajuda na depuração
        return f"DirNode({self.path!r}, size={self.size}, files={self.file_count})"


@dataclass(order=True)
class FileEntry:
    size: int
    path: str = field(compare=False)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass
class ScanResult:
    root: DirNode
    largest: List[FileEntry]
    inaccessible: int = 0
    links_skipped: int = 0
    dir_count: int = 0
    elapsed: float = 0.0

    def find_dir(self, path: str) -> Optional[DirNode]:
        """Procura a pasta ``path`` na árvore (None se não estiver dentro da raiz)."""
        root_norm = _norm(self.root.path)
        target = _norm(path)
        if target == root_norm:
            return self.root
        try:
            if os.path.commonpath([target, root_norm]) != root_norm:
                return None
        except ValueError:
            return None
        rel = os.path.relpath(target, root_norm)
        node = self.root
        for part in rel.split(os.sep):
            part = os.path.normcase(part)
            node = next((c for c in node.children if os.path.normcase(c.name) == part), None)
            if node is None:
                return None
        return node

    def remove_dir(self, node: DirNode) -> None:
        """Retira uma pasta da árvore e desconta o tamanho nas pastas-mãe."""
        parent = node.parent
        if parent is None:
            return
        try:
            parent.children.remove(node)
        except ValueError:
            return
        _subtract(parent, node.size, node.file_count)
        prefix = _norm(node.path) + os.sep
        self.largest = [f for f in self.largest if not _norm(f.path).startswith(prefix)]

    def remove_file(self, path: str, size: int) -> None:
        """Desconta um ficheiro removido (pasta-mãe e lista dos maiores)."""
        target = _norm(path)
        self.largest = [f for f in self.largest if _norm(f.path) != target]
        parent = self.find_dir(os.path.dirname(path))
        if parent is not None:
            parent.own_size = max(0, parent.own_size - size)
            parent.own_files = max(0, parent.own_files - 1)
            _subtract(parent, size, 1)


def _subtract(node: Optional[DirNode], size: int, files: int) -> None:
    while node is not None:
        node.size = max(0, node.size - size)
        node.file_count = max(0, node.file_count - files)
        node = node.parent


def _reparse_tag(st: os.stat_result) -> int:
    return int(getattr(st, "st_reparse_tag", 0) or 0)


def _is_cloud_tag(tag: int) -> bool:
    # IO_REPARSE_TAG_CLOUD e variantes (0x9000001A, 0x9000101A, … 0x9000F01A)
    return (tag & 0xFFFF0FFF) == 0x9000001A


def _classify(entry: os.DirEntry) -> Tuple[str, Optional[os.stat_result]]:
    """Devolve ("dir"|"file"|"link"|"error", stat) sem seguir links."""
    try:
        if entry.is_symlink():
            return "link", None
        is_junction = getattr(entry, "is_junction", None)
        if is_junction and is_junction():
            return "link", None
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return "error", None
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    is_dir = stat.S_ISDIR(st.st_mode)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        tag = _reparse_tag(st)
        if is_dir:
            # Pastas: só se entra em pastas sincronizadas com a nuvem (não são atalhos).
            if not _is_cloud_tag(tag):
                return "link", st
        elif tag in _LINK_TAGS or tag == 0:
            return "link", st
    return ("dir" if is_dir else "file"), st


def _drive_used_bytes(root: str) -> int:
    """Bytes usados na unidade se ``root`` for a raiz de uma unidade (para estimar o progresso)."""
    drive, rest = os.path.splitdrive(os.path.abspath(root))
    if rest.strip("\\/"):
        return 0
    try:
        return int(psutil.disk_usage(root).used)
    except Exception:  # noqa: BLE001
        return 0


def scan(
    root: str,
    progress: ProgressCallback = no_progress,
    should_cancel: CancelCheck = never_cancel,
    top_n: int = DEFAULT_TOP_FILES,
) -> ScanResult:
    """Analisa ``root`` e devolve a árvore de pastas e os ``top_n`` maiores ficheiros.

    Lança ``OperationCancelled`` se ``should_cancel()`` passar a True e
    ``DiskSpaceError`` se a raiz não for uma pasta (ou for um link).
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise DiskSpaceError(tr("A pasta não existe: {path}", path=root))
    try:
        root_st = os.lstat(root)
    except OSError as exc:
        raise DiskSpaceError(tr("A pasta não existe: {path}", path=root)) from exc
    root_attrs = int(getattr(root_st, "st_file_attributes", 0) or 0)
    if stat.S_ISLNK(root_st.st_mode) or (
        root_attrs & FILE_ATTRIBUTE_REPARSE_POINT and not _is_cloud_tag(_reparse_tag(root_st))
    ):
        raise DiskSpaceError(tr("A pasta escolhida é um atalho (symlink/junction) e não é seguida."))
    started = time.monotonic()
    root_node = DirNode(root, None, root)
    order: List[DirNode] = []  # pré-ordem; invertida dá a ordem de baixo para cima
    heap: List[Tuple[int, str]] = []
    inaccessible = 0
    links = 0
    scanned_bytes = 0
    used = _drive_used_bytes(root)
    last_report = 0.0

    stack: List[Tuple[DirNode, str]] = [(root_node, root)]
    while stack:
        if should_cancel():
            raise OperationCancelled()
        node, path = stack.pop()
        order.append(node)
        now = time.monotonic()
        if now - last_report >= PROGRESS_INTERVAL:
            last_report = now
            pct = min(99, int(scanned_bytes * 100 / used)) if used else 0
            progress(pct, path)
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            node.inaccessible = True
            inaccessible += 1
            continue
        for entry in entries:
            kind, st = _classify(entry)
            if kind == "dir":
                child = DirNode(entry.name, node)
                node.children.append(child)
                stack.append((child, entry.path))
            elif kind == "file":
                attrs = int(getattr(st, "st_file_attributes", 0) or 0)
                size = 0 if attrs & _NOT_LOCAL else int(st.st_size)  # ficheiros só na nuvem não ocupam disco
                node.own_size += size
                node.own_files += 1
                scanned_bytes += size
                if size > 0 and top_n > 0:
                    if len(heap) < top_n:
                        heapq.heappush(heap, (size, entry.path))
                    elif size > heap[0][0]:
                        heapq.heapreplace(heap, (size, entry.path))
            elif kind == "link":
                links += 1
            else:
                inaccessible += 1

    # Agregar de baixo para cima (cada filho aparece depois do pai na pré-ordem).
    for node in reversed(order):
        node.size += node.own_size
        node.file_count += node.own_files
        if node.parent is not None:
            node.parent.size += node.size
            node.parent.file_count += node.file_count

    largest = [FileEntry(size, path) for size, path in sorted(heap, reverse=True)]
    progress(100, root)
    result = ScanResult(root_node, largest, inaccessible, links, len(order), time.monotonic() - started)
    log.info("Análise de %s: %s em %d ficheiros, %d pastas, %d inacessíveis, %d links ignorados (%.1fs)",
             root, format_bytes(root_node.size), root_node.file_count, result.dir_count,
             inaccessible, links, result.elapsed)
    return result


# --------------------------------------------------------------------------- #
# Reciclagem
# --------------------------------------------------------------------------- #
_ROOT_SYSTEM_NAMES = {
    "windows", "program files", "program files (x86)", "programdata", "users", "boot", "recovery",
    "system volume information", "pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr",
    "bootnxt", "dumpstack.log", "dumpstack.log.tmp", "perflogs", "documents and settings",
}


def _is_inside_or_equal(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:
        return False


def protected_reason(path: str) -> Optional[str]:
    """Devolve o motivo (traduzido) para não enviar ``path`` para a Reciclagem, ou None se for permitido."""
    if not path or not os.path.isabs(path):
        return tr("Caminho inválido.")
    if not os.path.lexists(path):
        return tr("O item já não existe.")
    norm = _norm(path)
    drive, rest = os.path.splitdrive(norm)
    parts = [p for p in rest.replace("/", "\\").split("\\") if p]
    if not parts:
        return tr("Não é possível enviar a raiz de uma unidade para a Reciclagem.")
    if len(parts) == 1 and (parts[0] in _ROOT_SYSTEM_NAMES or parts[0].startswith("$")):
        return tr("É uma pasta ou ficheiro do sistema na raiz da unidade.")
    try:
        st = os.lstat(path)
    except OSError as exc:
        return tr("Não foi possível ler o item: {erro}", erro=exc.strerror or str(exc))
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    is_link_dir = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) and stat.S_ISDIR(st.st_mode) \
        and not _is_cloud_tag(_reparse_tag(st))
    if stat.S_ISLNK(st.st_mode) or is_link_dir:
        return tr("É um atalho do sistema (symlink/junction); o SafeOti não mexe em ligações.")
    if attrs & FILE_ATTRIBUTE_SYSTEM:
        return tr("O item está marcado como ficheiro de sistema do Windows.")

    windir = env_path("SystemRoot") or env_path("WINDIR")
    if windir and _is_inside_or_equal(norm, _norm(windir)):
        return tr("Está dentro da pasta do Windows.")
    for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = env_path(name)
        if value and _is_inside_or_equal(norm, _norm(value)):
            return tr("Está dentro de Programas; para remover uma aplicação use 'Aplicações' nas Definições do Windows.")
    for protected in _forbidden_roots():
        if _is_inside_or_equal(protected, norm):
            return tr("É (ou contém) uma pasta protegida do sistema ou do utilizador.")
    return None


def recycle(path: str) -> None:
    """Envia ``path`` para a Reciclagem do Windows. Lança OSError se falhar ou for cancelado."""
    reason = protected_reason(path)
    if reason:
        raise DiskSpaceError(reason)
    if not IS_WINDOWS:
        raise DiskSpaceError(tr("A Reciclagem só está disponível no Windows."))
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 0x0003
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMMKDIR = 0x0200
    # O Windows avisa (e deixa cancelar) se o item não couber na Reciclagem e fosse apagado de vez.
    FOF_WANTNUKEWARNING = 0x4000

    full = os.path.normpath(os.path.abspath(path))
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = full + "\0"  # o ctypes junta o segundo terminador
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOCONFIRMMKDIR | FOF_WANTNUKEWARNING
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    code = shell32.SHFileOperationW(ctypes.byref(op))
    if op.fAnyOperationsAborted:
        raise OperationCancelled()
    if code != 0 or os.path.lexists(full):
        raise DiskSpaceError(tr("O Windows não conseguiu enviar o item para a Reciclagem (código {code}).",
                         code=f"0x{code:X}"))
    log.info("Enviado para a Reciclagem: %s", full)
