"""Análise e limpeza de ficheiros temporários.

Regras de segurança:
* só se apaga conteúdo dentro das pastas-raiz definidas em ``_targets_for``;
* a própria pasta-raiz nunca é apagada;
* atalhos simbólicos, symlinks e junctions (reparse points) nunca são seguidos
  nem apagados;
* ficheiros em uso ou sem permissão são ignorados sem interromper a limpeza;
* a Reciclagem é esvaziada através da API do Windows (SHEmptyRecycleBinW).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import stat
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

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

log = logging.getLogger("safeoti.cleaner")

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


# --------------------------------------------------------------------------- #
# Categorias
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CleanCategory:
    id: str
    # Texto original em português: a tabela é criada na importação, antes de se escolher o idioma.
    source_name: str
    source_description: str
    requires_admin: bool = False
    is_recycle_bin: bool = False
    default_selected: bool = True

    @property
    def name(self) -> str:
        """Nome traduzido para o idioma ativo (só para mostrar)."""
        return tr(self.source_name)

    @property
    def description(self) -> str:
        """Descrição traduzida para o idioma ativo (só para mostrar)."""
        return tr(self.source_description)


CATEGORIES: List[CleanCategory] = [
    CleanCategory("user_temp", "Temp do utilizador", "Ficheiros temporários da conta atual (%TEMP%)."),
    CleanCategory(
        "windows_temp",
        "Temp do Windows",
        "Ficheiros temporários do sistema (C:\\Windows\\Temp).",
        requires_admin=True,
    ),
    CleanCategory(
        "thumbnails",
        "Cache de miniaturas",
        "Miniaturas e ícones em cache do Explorador (são recriados automaticamente).",
    ),
    CleanCategory("chrome", "Cache do Google Chrome", "Cache de páginas, código e GPU de todos os perfis."),
    CleanCategory("edge", "Cache do Microsoft Edge", "Cache de páginas, código e GPU de todos os perfis."),
    CleanCategory("firefox", "Cache do Mozilla Firefox", "Cache de páginas de todos os perfis."),
    CleanCategory(
        "windows_update",
        "Cache do Windows Update",
        "Atualizações já transferidas (SoftwareDistribution\\Download).",
        requires_admin=True,
        default_selected=False,
    ),
    CleanCategory(
        "recycle_bin",
        "Reciclagem",
        "Esvazia a Reciclagem de todas as unidades. Os ficheiros deixam de poder ser recuperados.",
        is_recycle_bin=True,
        default_selected=False,
    ),
]

CATEGORY_BY_ID: Dict[str, CleanCategory] = {c.id: c for c in CATEGORIES}


@dataclass(frozen=True)
class Target:
    """Pasta-raiz a limpar. Só o conteúdo é apagado, nunca a raiz."""

    root: str
    patterns: Optional[Tuple[str, ...]] = None  # padrões de nome de ficheiro (None = todos)
    recursive: bool = True


@dataclass
class CategoryScan:
    category_id: str
    files: int = 0
    bytes: int = 0
    inaccessible: int = 0
    roots: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class CategoryClean:
    category_id: str
    deleted_files: int = 0
    freed_bytes: int = 0
    skipped_files: int = 0
    removed_dirs: int = 0
    error: Optional[str] = None


@dataclass
class CleanSummary:
    results: List[CategoryClean] = field(default_factory=list)
    cancelled: bool = False

    @property
    def freed_bytes(self) -> int:
        return sum(r.freed_bytes for r in self.results)

    @property
    def deleted_files(self) -> int:
        return sum(r.deleted_files for r in self.results)

    @property
    def skipped_files(self) -> int:
        return sum(r.skipped_files for r in self.results)


# --------------------------------------------------------------------------- #
# Localização das pastas
# --------------------------------------------------------------------------- #
def _chromium_profiles(user_data: str) -> List[str]:
    """Pastas de perfil de um browser baseado em Chromium."""
    profiles: List[str] = []
    try:
        with os.scandir(user_data) as it:
            for entry in it:
                if not _is_plain_dir(entry):
                    continue
                name = entry.name
                if name == "Default" or name.startswith("Profile ") or name in ("Guest Profile", "System Profile"):
                    profiles.append(entry.path)
    except OSError:
        pass
    return sorted(profiles)


def _chromium_targets(user_data: str) -> List[Target]:
    targets: List[Target] = []
    for profile in _chromium_profiles(user_data):
        for sub in ("Cache", "Code Cache", "GPUCache"):
            targets.append(Target(os.path.join(profile, sub)))
    for sub in ("ShaderCache", "GrShaderCache"):
        targets.append(Target(os.path.join(user_data, sub)))
    return targets


def _firefox_targets(local_appdata: str) -> List[Target]:
    targets: List[Target] = []
    profiles_dir = os.path.join(local_appdata, "Mozilla", "Firefox", "Profiles")
    try:
        with os.scandir(profiles_dir) as it:
            for entry in it:
                if _is_plain_dir(entry):
                    targets.append(Target(os.path.join(entry.path, "cache2")))
    except OSError:
        pass
    return targets


def _targets_for(category_id: str) -> List[Target]:
    local = env_path("LOCALAPPDATA")
    windir = env_path("SystemRoot", env_path("WINDIR", "C:\\Windows"))
    if category_id == "user_temp":
        temp = env_path("TEMP") or env_path("TMP") or tempfile.gettempdir()
        return [Target(temp)]
    if category_id == "windows_temp":
        return [Target(os.path.join(windir, "Temp"))]
    if category_id == "thumbnails":
        if not local:
            return []
        return [
            Target(
                os.path.join(local, "Microsoft", "Windows", "Explorer"),
                patterns=("thumbcache_*.db", "iconcache_*.db"),
                recursive=False,
            )
        ]
    if category_id == "chrome":
        return _chromium_targets(os.path.join(local, "Google", "Chrome", "User Data")) if local else []
    if category_id == "edge":
        return _chromium_targets(os.path.join(local, "Microsoft", "Edge", "User Data")) if local else []
    if category_id == "firefox":
        return _firefox_targets(local) if local else []
    if category_id == "windows_update":
        return [Target(os.path.join(windir, "SoftwareDistribution", "Download"))]
    return []


# --------------------------------------------------------------------------- #
# Segurança de caminhos
# --------------------------------------------------------------------------- #
def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _forbidden_roots() -> set:
    """Pastas que nunca podem ser usadas como raiz de limpeza (proteção contra variáveis mal definidas)."""
    names = [
        "SystemRoot", "WINDIR", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
        "ProgramData", "APPDATA", "LOCALAPPDATA", "PUBLIC",
    ]
    result = set()
    for name in names:
        value = env_path(name)
        if value:
            result.add(_norm(value))
    home = os.path.expanduser("~")
    result.add(_norm(home))
    for sub in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "OneDrive"):
        result.add(_norm(os.path.join(home, sub)))
    windir = env_path("SystemRoot")
    if windir:
        result.add(_norm(os.path.join(windir, "System32")))
        result.add(_norm(os.path.join(windir, "SysWOW64")))
    return result


def _has_reparse_attr(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link_path(path: str) -> bool:
    """True se o caminho for symlink, junction ou outro reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return True  # na dúvida, tratar como inseguro
    if stat.S_ISLNK(st.st_mode) or _has_reparse_attr(st):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _is_link_entry(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        if is_junction and is_junction():
            return True
        st = entry.stat(follow_symlinks=False)
        return _has_reparse_attr(st)
    except OSError:
        return True


def _is_plain_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False) and not _is_link_entry(entry)
    except OSError:
        return False


def is_safe_root(root: str) -> Tuple[bool, str]:
    """Valida uma pasta-raiz antes de a analisar ou limpar."""
    if not root or not os.path.isabs(root):
        return False, "caminho inválido"
    norm = _norm(root)
    drive, rest = os.path.splitdrive(norm)
    parts = [p for p in rest.replace("/", "\\").split("\\") if p]
    if len(parts) < 2:
        return False, "pasta demasiado próxima da raiz da unidade"
    if norm in _forbidden_roots():
        return False, "pasta protegida do sistema ou do utilizador"
    if not os.path.isdir(root):
        return False, "pasta inexistente"
    if _is_link_path(root):
        return False, "a pasta é um atalho/symlink/junction"
    return True, ""


def is_within(path: str, root: str) -> bool:
    """True se ``path`` estiver estritamente dentro de ``root``."""
    p, r = _norm(path), _norm(root)
    if p == r:
        return False
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # unidades diferentes
        return False


# --------------------------------------------------------------------------- #
# Percorrer pastas
# --------------------------------------------------------------------------- #
@dataclass
class _WalkStats:
    inaccessible: int = 0


def _walk_target(
    target: Target,
    dirs_out: Optional[List[str]],
    stats: _WalkStats,
    should_cancel: CancelCheck,
) -> Iterator[Tuple[str, int]]:
    """Percorre uma raiz sem seguir links e devolve (caminho, tamanho) de cada ficheiro.

    As subpastas visitadas são acrescentadas a ``dirs_out`` (pela ordem de visita),
    para poderem ser removidas depois, de baixo para cima, se ficarem vazias.
    """
    stack = [target.root]
    while stack:
        if should_cancel():
            raise OperationCancelled()
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            stats.inaccessible += 1
            continue
        for entry in entries:
            if _is_link_entry(entry):
                continue  # nunca seguir nem apagar links/junctions
            try:
                if entry.is_dir(follow_symlinks=False):
                    if target.recursive and target.patterns is None:
                        stack.append(entry.path)
                        if dirs_out is not None:
                            dirs_out.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if target.patterns and not any(fnmatch.fnmatch(entry.name.lower(), p) for p in target.patterns):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                stats.inaccessible += 1
                continue
            yield entry.path, size


def _valid_targets(category_id: str) -> Tuple[List[Target], List[str]]:
    targets: List[Target] = []
    notes: List[str] = []
    seen = set()
    for target in _targets_for(category_id):
        key = _norm(target.root)
        if key in seen:
            continue
        seen.add(key)
        ok, reason = is_safe_root(target.root)
        if ok:
            targets.append(target)
        elif reason != "pasta inexistente":
            notes.append(f"{target.root}: {tr(reason)}")
            log.warning("Pasta ignorada na categoria %s: %s (%s)", category_id, target.root, reason)
    return targets, notes


# --------------------------------------------------------------------------- #
# Reciclagem (API do Windows)
# --------------------------------------------------------------------------- #
SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004


def _recycle_bin_query() -> Tuple[int, int]:
    """Devolve (bytes, itens) na Reciclagem de todas as unidades."""
    if not IS_WINDOWS:
        return 0, 0
    import ctypes
    from ctypes import wintypes

    class SHQUERYRBINFO(ctypes.Structure):
        if ctypes.sizeof(ctypes.c_void_p) == 4:
            _pack_ = 1
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]

    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    fn = ctypes.windll.shell32.SHQueryRecycleBinW
    fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO)]
    fn.restype = ctypes.c_long
    hr = fn(None, ctypes.byref(info))
    if hr != 0:
        raise OSError(f"SHQueryRecycleBinW falhou (0x{hr & 0xFFFFFFFF:08X})")
    return int(info.i64Size), int(info.i64NumItems)


def _recycle_bin_empty() -> None:
    if not IS_WINDOWS:
        return
    import ctypes
    from ctypes import wintypes

    fn = ctypes.windll.shell32.SHEmptyRecycleBinW
    fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
    fn.restype = ctypes.c_long
    hr = fn(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    # E_UNEXPECTED é devolvido quando a Reciclagem já está vazia.
    if hr not in (0, -2147418113):
        raise OSError(f"SHEmptyRecycleBinW falhou (0x{hr & 0xFFFFFFFF:08X})")


# --------------------------------------------------------------------------- #
# Análise
# --------------------------------------------------------------------------- #
def scan_category(category_id: str, should_cancel: CancelCheck = never_cancel,
                  on_file: Optional[callable] = None) -> CategoryScan:
    category = CATEGORY_BY_ID[category_id]
    result = CategoryScan(category_id)
    if category.is_recycle_bin:
        try:
            size, items = _recycle_bin_query()
            result.bytes, result.files = size, items
        except OSError as exc:
            result.error = tr("Não foi possível consultar a Reciclagem.")
            log.error("Erro ao consultar a Reciclagem: %s", exc)
        return result

    targets, notes = _valid_targets(category_id)
    result.roots = [t.root for t in targets]
    if notes:
        result.error = "; ".join(notes)
    stats = _WalkStats()
    for target in targets:
        for _path, size in _walk_target(target, None, stats, should_cancel):
            result.files += 1
            result.bytes += size
            if on_file and result.files % 500 == 0:
                on_file(result)
    result.inaccessible = stats.inaccessible
    return result


def scan(
    category_ids: Sequence[str],
    progress: ProgressCallback = no_progress,
    should_cancel: CancelCheck = never_cancel,
) -> Dict[str, CategoryScan]:
    """Analisa as categorias indicadas sem apagar nada."""
    results: Dict[str, CategoryScan] = {}
    total = max(1, len(category_ids))
    log.info("Análise iniciada: %s", ", ".join(category_ids))
    for index, cat_id in enumerate(category_ids):
        name = CATEGORY_BY_ID[cat_id].name
        base = int(index * 100 / total)
        progress(base, tr("A analisar: {name}…", name=name))

        def on_file(partial: CategoryScan, _name=name, _base=base) -> None:
            progress(_base, tr("A analisar: {name} — {files} ficheiros ({size})", name=_name, files=partial.files, size=format_bytes(partial.bytes)))

        results[cat_id] = scan_category(cat_id, should_cancel, on_file)
        r = results[cat_id]
        log.info("Análise %s: %d ficheiros, %s", cat_id, r.files, format_bytes(r.bytes))
    progress(100, tr("Análise concluída."))
    return results


# --------------------------------------------------------------------------- #
# Limpeza
# --------------------------------------------------------------------------- #
def _delete_file(path: str, root: str) -> bool:
    """Apaga um ficheiro dentro de ``root``. Devolve True se foi apagado."""
    if not is_within(path, root) or _is_link_path(path):
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except PermissionError:
        # Ficheiros só de leitura: retirar o atributo e tentar outra vez.
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.remove(path)
            return True
        except OSError:
            return False
    except OSError:
        return False  # em uso por outro processo, etc.


def _remove_empty_dirs(dirs: Iterable[str], root: str) -> int:
    removed = 0
    for directory in sorted(dirs, key=lambda d: d.count(os.sep), reverse=True):
        if not is_within(directory, root) or _is_link_path(directory):
            continue
        try:
            os.rmdir(directory)  # só remove se estiver vazia
            removed += 1
        except OSError:
            pass
    return removed


def clean_category(
    category_id: str,
    should_cancel: CancelCheck = never_cancel,
    on_file: Optional[callable] = None,
    result: Optional[CategoryClean] = None,
) -> CategoryClean:
    """Limpa uma categoria. Se for cancelada, ``result`` fica com o progresso parcial."""
    category = CATEGORY_BY_ID[category_id]
    result = result if result is not None else CategoryClean(category_id)

    if category.is_recycle_bin:
        try:
            before_size, before_items = _recycle_bin_query()
            if before_items == 0:
                return result
            _recycle_bin_empty()
            after_size, after_items = _recycle_bin_query()
            result.freed_bytes = max(0, before_size - after_size)
            result.deleted_files = max(0, before_items - after_items)
            result.skipped_files = after_items
        except OSError as exc:
            result.error = tr("Não foi possível esvaziar a Reciclagem.")
            log.error("Erro ao esvaziar a Reciclagem: %s", exc)
        return result

    targets, notes = _valid_targets(category_id)
    if notes:
        result.error = "; ".join(notes)
    for target in targets:
        stats = _WalkStats()
        dirs: List[str] = []
        for path, size in _walk_target(target, dirs, stats, should_cancel):
            if _delete_file(path, target.root):
                result.deleted_files += 1
                result.freed_bytes += size
            else:
                result.skipped_files += 1
            if on_file and (result.deleted_files + result.skipped_files) % 200 == 0:
                on_file(result)
        result.skipped_files += stats.inaccessible
        if target.recursive and target.patterns is None:
            result.removed_dirs += _remove_empty_dirs(dirs, target.root)
    return result


def clean(
    category_ids: Sequence[str],
    progress: ProgressCallback = no_progress,
    should_cancel: CancelCheck = never_cancel,
    expected_files: Optional[Dict[str, int]] = None,
) -> CleanSummary:
    """Apaga o conteúdo das categorias indicadas. Ficheiros em uso são ignorados."""
    summary = CleanSummary()
    expected_files = expected_files or {}
    total_expected = sum(expected_files.get(c, 0) for c in category_ids)
    processed_before = 0
    total = max(1, len(category_ids))
    log.info("Limpeza iniciada: %s", ", ".join(category_ids))
    try:
        for index, cat_id in enumerate(category_ids):
            name = CATEGORY_BY_ID[cat_id].name
            base_by_category = int(index * 100 / total)
            progress(base_by_category, tr("A limpar: {name}…", name=name))

            def on_file(partial: CategoryClean, _name=name, _idx=index, _before=processed_before) -> None:
                done = partial.deleted_files + partial.skipped_files
                if total_expected > 0:
                    pct = min(99, int((_before + done) * 100 / total_expected))
                else:
                    pct = int(_idx * 100 / total)
                progress(pct, tr("A limpar: {name} — {count} ficheiros apagados", name=_name, count=partial.deleted_files))

            result = CategoryClean(cat_id)
            summary.results.append(result)  # antes de limpar, para guardar o parcial se cancelar
            clean_category(cat_id, should_cancel, on_file, result)
            processed_before += result.deleted_files + result.skipped_files
            log.info(
                "Limpeza %s: %d ficheiros apagados, %s libertados, %d ignorados",
                cat_id, result.deleted_files, format_bytes(result.freed_bytes), result.skipped_files,
            )
    except OperationCancelled:
        summary.cancelled = True
        log.info("Limpeza cancelada pelo utilizador.")
    progress(100, tr("Limpeza concluída.") if not summary.cancelled else tr("Limpeza cancelada."))
    log.info(
        "Limpeza terminada: %s libertados, %d ficheiros apagados, %d ignorados",
        format_bytes(summary.freed_bytes), summary.deleted_files, summary.skipped_files,
    )
    return summary
