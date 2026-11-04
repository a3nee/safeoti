"""Procura de atualizações através das GitHub Releases (sem Qt, sem dependências novas).

Fluxo típico:

1. ``check_latest()`` consulta ``/repos/{repo}/releases/latest`` e devolve um
   ``CheckResult`` (sem versões publicadas / já atualizado / há atualização).
2. ``should_notify()`` decide se o utilizador deve ser avisado (respeita a
   versão que ele mandou ignorar).
3. ``download_release()`` transfere o instalador para uma pasta temporária,
   confirma o tamanho e, se a versão publicar um SHA-256, confirma-o também.
4. ``launch_installer()`` abre o instalador transferido; quem chama decide
   quando fechar o SafeOti (o instalador recusa instalar com o SafeOti aberto).

Só são aceites transferências por HTTPS a partir de anfitriões do GitHub.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

from . import __version__
from .i18n import tr
from .utils import CancelCheck, OperationCancelled, ProgressCallback, format_bytes, never_cancel, no_progress

log = logging.getLogger("safeoti.updater")

UPDATE_REPO = "a3nee/safeoti"
API_URL = "https://api.github.com/repos/{repo}/releases/latest"
RELEASES_PAGE = "https://github.com/{repo}/releases"
USER_AGENT = f"SafeOti-Updater/{__version__}"

# Anfitriões de onde se aceita transferir (inclui os redirecionamentos do GitHub
# para o armazenamento dos ficheiros das versões).
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

# Ordem de preferência do ficheiro a transferir.
ASSET_PATTERNS = ("safeoti-setup*.exe", "safeoti*.exe")

DEFAULT_TIMEOUT = 10.0
CHUNK_SIZE = 64 * 1024
MAX_API_BYTES = 2 * 1024 * 1024
MAX_SHA_FILE_BYTES = 64 * 1024
MAX_NOTES_CHARS = 20000

STATUS_NO_RELEASES = "no_releases"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_UPDATE = "update_available"


class UpdateError(Exception):
    """Erro da atualização, já com uma mensagem compreensível (pt, traduzida)."""


# --------------------------------------------------------------------- versões

_VERSION_RE = re.compile(r"^\s*[vV]?\s*(\d+(?:\.\d+)*)\s*(?:[-_.]?\s*([0-9A-Za-z.\-]+?))?\s*(?:\+[0-9A-Za-z.\-]*)?\s*$")


@dataclass(frozen=True)
class Version:
    """Versão do tipo ``1.2.3`` ou ``1.2.3-beta.1`` (pré-versões ficam abaixo da final)."""

    numbers: Tuple[int, ...]
    prerelease: Tuple[Any, ...] = ()
    text: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def _key(self) -> tuple:
        nums = tuple(self.numbers)
        while len(nums) > 1 and nums[-1] == 0:  # 1.2 == 1.2.0
            nums = nums[:-1]
        pre = tuple((0, p, "") if isinstance(p, int) else (1, 0, p) for p in self.prerelease)
        # Uma versão final (sem pré-versão) é maior do que qualquer pré-versão.
        return (nums, 0 if pre else 1, pre)

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: "Version") -> bool:
        return self._key() > other._key()

    def __ge__(self, other: "Version") -> bool:
        return self._key() >= other._key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return self.text or ".".join(str(n) for n in self.numbers)


def parse_version(text: str) -> Optional[Version]:
    """Interpreta ``"1.2.3"``, ``"1.2"``, ``"v1.2.3-beta"``…; devolve None se não for uma versão."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text)
    if not match:
        return None
    numbers = tuple(int(n) for n in match.group(1).split("."))
    pre_text = (match.group(2) or "").strip(".-")
    prerelease: Tuple[Any, ...] = ()
    if pre_text:
        parts = [p for p in re.split(r"[.\-]", pre_text) if p]
        prerelease = tuple(int(p) if p.isdigit() else p.lower() for p in parts)
    return Version(numbers, prerelease, normalize_tag(text))


def normalize_tag(tag: str) -> str:
    """``"v1.2.3"`` → ``"1.2.3"``."""
    tag = (tag or "").strip()
    if tag[:1] in ("v", "V") and tag[1:2].isdigit():
        tag = tag[1:]
    return tag


def is_newer(latest: str, current: str = __version__) -> bool:
    """True se ``latest`` for estritamente mais recente do que ``current``."""
    new = parse_version(latest)
    cur = parse_version(current)
    if new is None:
        return False
    if cur is None:  # versão atual ilegível: qualquer versão válida conta como nova
        return True
    return new > cur


# ---------------------------------------------------------------- versões (GitHub)

@dataclass
class ReleaseAsset:
    name: str
    url: str
    size: int = 0
    content_type: str = ""


@dataclass
class ReleaseInfo:
    version: str  # sem o "v" inicial
    tag: str
    name: str = ""
    notes: str = ""
    html_url: str = ""
    published_at: str = ""
    draft: bool = False
    prerelease: bool = False
    asset: Optional[ReleaseAsset] = None
    sha256_asset: Optional[ReleaseAsset] = None
    sha256: str = ""  # SHA-256 indicado no texto da versão (se existir)
    assets: List[ReleaseAsset] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.name or self.tag or self.version

    @property
    def is_installer(self) -> bool:
        return bool(self.asset) and fnmatch.fnmatch(self.asset.name.lower(), "safeoti-setup*.exe")


@dataclass
class CheckResult:
    status: str
    current: str = __version__
    release: Optional[ReleaseInfo] = None

    @property
    def update_available(self) -> bool:
        return self.status == STATUS_UPDATE


def select_asset(assets: List[ReleaseAsset]) -> Optional[ReleaseAsset]:
    """Prefere ``SafeOti-Setup*.exe``; senão ``SafeOti*.exe``."""
    for pattern in ASSET_PATTERNS:
        for asset in assets:
            if fnmatch.fnmatch(asset.name.lower(), pattern):
                return asset
    return None


def select_sha256_asset(assets: List[ReleaseAsset], target: Optional[ReleaseAsset]) -> Optional[ReleaseAsset]:
    """Ficheiro ``*.sha256`` correspondente ao ficheiro escolhido (ou o único que exista)."""
    shas = [a for a in assets if a.name.lower().endswith((".sha256", ".sha256sum"))]
    if not shas:
        return None
    if target is not None:
        base = target.name.lower()
        for asset in shas:
            if asset.name.lower() in (base + ".sha256", base + ".sha256sum"):
                return asset
        stem = os.path.splitext(base)[0]
        for asset in shas:
            if os.path.splitext(asset.name.lower())[0] in (stem, base):
                return asset
    return shas[0] if len(shas) == 1 else None


_HEX64 = re.compile(r"\b([0-9a-fA-F]{64})\b")
_BODY_SHA = re.compile(r"SHA-?256[^:\n]*:\s*[`*]*\s*([0-9a-fA-F]{64})\b", re.IGNORECASE)


def parse_sha256_text(text: str, filename: str = "") -> str:
    """Extrai o hash de um ficheiro ``.sha256`` (formato ``<hex>  nome`` ou só ``<hex>``)."""
    if not text:
        return ""
    found: List[Tuple[str, str]] = []
    for line in text.splitlines():
        match = _HEX64.search(line)
        if match:
            rest = line[match.end():].strip().lstrip("*").strip()
            found.append((match.group(1).lower(), rest))
    if not found:
        return ""
    if filename:
        for digest, rest in found:
            if rest and os.path.basename(rest).lower() == filename.lower():
                return digest
    return found[0][0]


def parse_sha256_from_body(body: str, filename: str = "") -> str:
    """Procura linhas ``SHA256: <hex>`` nas notas da versão (preferindo a que cita o ficheiro)."""
    if not body:
        return ""
    first = ""
    for line in body.splitlines():
        match = _BODY_SHA.search(line)
        if not match:
            continue
        digest = match.group(1).lower()
        if filename and filename.lower() in line.lower():
            return digest
        first = first or digest
    return first


def parse_release(data: dict) -> ReleaseInfo:
    """Converte a resposta JSON da API numa ``ReleaseInfo``."""
    if not isinstance(data, dict):
        raise UpdateError(tr("Resposta inesperada do GitHub."))
    tag = str(data.get("tag_name") or "").strip()
    assets: List[ReleaseAsset] = []
    for raw in data.get("assets") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        url = str(raw.get("browser_download_url") or "")
        if not name or not url:
            continue
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        assets.append(ReleaseAsset(name, url, size, str(raw.get("content_type") or "")))
    asset = select_asset(assets)
    body = str(data.get("body") or "")
    return ReleaseInfo(
        version=normalize_tag(tag),
        tag=tag,
        name=str(data.get("name") or "").strip(),
        notes=body[:MAX_NOTES_CHARS],
        html_url=str(data.get("html_url") or ""),
        published_at=str(data.get("published_at") or ""),
        draft=bool(data.get("draft")),
        prerelease=bool(data.get("prerelease")),
        asset=asset,
        sha256_asset=select_sha256_asset(assets, asset),
        sha256=parse_sha256_from_body(body, asset.name if asset else ""),
        assets=assets,
    )


def releases_page(repo: str = UPDATE_REPO) -> str:
    return RELEASES_PAGE.format(repo=repo)


# ------------------------------------------------------------------------ rede

def is_allowed_download_url(url: str) -> bool:
    """Só HTTPS e só anfitriões do GitHub (sem utilizador/palavra-passe no URL)."""
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and host in ALLOWED_DOWNLOAD_HOSTS
        and port in (None, 443)
        and not parts.username
        and not parts.password
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Recusa redirecionamentos para fora dos anfitriões permitidos."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if not is_allowed_download_url(newurl):
            raise UpdateError(tr("A transferência foi redirecionada para um endereço não confiável."))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _network_error(exc: BaseException) -> UpdateError:
    log.warning("Erro de rede na atualização: %r", exc)
    if isinstance(exc, UpdateError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return UpdateError(tr("O GitHub limitou temporariamente os pedidos. Tente novamente mais tarde."))
        return UpdateError(tr("O GitHub respondeu com um erro (HTTP {codigo}).", codigo=exc.code))
    if isinstance(exc, ssl.SSLError) or isinstance(getattr(exc, "reason", None), ssl.SSLError):
        return UpdateError(tr("Não foi possível estabelecer uma ligação segura ao GitHub."))
    if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
        return UpdateError(tr("O GitHub demorou demasiado a responder. Tente novamente mais tarde."))
    return UpdateError(tr("Não foi possível contactar o GitHub. Verifique a ligação à Internet."))


def _headers(accept: str) -> dict:
    return {"Accept": accept, "User-Agent": USER_AGENT}


def check_latest(timeout: float = DEFAULT_TIMEOUT, repo: str = UPDATE_REPO,
                 current: str = __version__) -> CheckResult:
    """Consulta a última versão publicada. 404 → ``STATUS_NO_RELEASES`` (não é erro).

    Lança ``UpdateError`` (mensagem já traduzida) em erros de rede ou de resposta.
    """
    url = API_URL.format(repo=repo)
    request = urllib.request.Request(url, headers={**_headers("application/vnd.github+json"),
                                                   "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_API_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return CheckResult(STATUS_NO_RELEASES, current)
        raise _network_error(exc) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _network_error(exc) from exc
    if len(raw) > MAX_API_BYTES:
        raise UpdateError(tr("Resposta inesperada do GitHub."))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError(tr("Resposta inesperada do GitHub.")) from exc
    release = parse_release(data)
    return evaluate(release, current)


def evaluate(release: ReleaseInfo, current: str = __version__) -> CheckResult:
    """Classifica uma versão publicada face à versão atual (ignora rascunhos e pré-versões)."""
    parsed = parse_version(release.version)
    if release.draft or release.prerelease or parsed is None or parsed.is_prerelease:
        return CheckResult(STATUS_NO_RELEASES if parsed is None else STATUS_UP_TO_DATE, current, release)
    if is_newer(release.version, current):
        return CheckResult(STATUS_UPDATE, current, release)
    return CheckResult(STATUS_UP_TO_DATE, current, release)


def should_notify(result: CheckResult, skipped_version: str = "", manual: bool = False) -> bool:
    """Avisar o utilizador? Na verificação automática respeita a versão ignorada."""
    if not result.update_available or result.release is None:
        return False
    if manual:
        return True
    skipped = parse_version(skipped_version or "")
    latest = parse_version(result.release.version)
    if skipped is not None and latest is not None and latest <= skipped:
        return False
    return True


# ---------------------------------------------------------------- transferência

def download_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "SafeOti-update")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_filename(name: str) -> str:
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._\-]", "_", name).strip(". ")
    return name or "SafeOti-Setup.exe"


def _open_download(url: str, timeout: float):
    if not is_allowed_download_url(url):
        raise UpdateError(tr("Endereço de transferência não confiável: {url}", url=url))
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(url, headers=_headers("application/octet-stream"))
    try:
        response = opener.open(request, timeout=timeout)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _network_error(exc) from exc
    final = response.geturl() if hasattr(response, "geturl") else url
    if final and not is_allowed_download_url(final):
        response.close()
        raise UpdateError(tr("A transferência foi redirecionada para um endereço não confiável."))
    return response


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("Não foi possível apagar %s", path)


def download_asset(url: str, dest: str, progress: ProgressCallback = no_progress,
                   should_cancel: CancelCheck = never_cancel, expected_size: int = 0,
                   expected_sha256: str = "", timeout: float = 30.0) -> str:
    """Transfere ``url`` para ``dest`` (via ``dest.part``) e confirma tamanho e SHA-256.

    Em caso de discrepância apaga o ficheiro e lança ``UpdateError``; se o
    utilizador cancelar, apaga o ficheiro parcial e lança ``OperationCancelled``.
    """
    part = dest + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    _remove(part)
    digest = hashlib.sha256()
    received = 0
    name = os.path.basename(dest)
    try:
        with _open_download(url, timeout) as response:
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError, AttributeError):
                total = 0
            total = expected_size or total
            with open(part, "wb") as handle:
                while True:
                    if should_cancel():
                        raise OperationCancelled()
                    try:
                        chunk = response.read(CHUNK_SIZE)
                    except (OSError, ValueError) as exc:
                        raise _network_error(exc) from exc
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if expected_size and received > expected_size:
                        raise UpdateError(tr("O ficheiro transferido é maior do que o esperado."))
                    if total:
                        pct = min(99, int(received * 100 / total))
                        progress(pct, tr("A transferir {nome}: {feito} de {total}", nome=name,
                                         feito=format_bytes(received), total=format_bytes(total)))
                    else:
                        progress(0, tr("A transferir {nome}: {feito}", nome=name, feito=format_bytes(received)))
        if expected_size and received != expected_size:
            raise UpdateError(tr("A transferência ficou incompleta ({feito} de {total}).",
                                 feito=format_bytes(received), total=format_bytes(expected_size)))
        if received == 0:
            raise UpdateError(tr("A transferência ficou incompleta ({feito} de {total}).",
                                 feito=format_bytes(0), total=format_bytes(expected_size)))
        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.strip().lower():
            raise UpdateError(tr("O ficheiro transferido não corresponde ao SHA-256 publicado. Foi apagado por segurança."))
        _remove(dest)
        os.replace(part, dest)
    except BaseException:
        _remove(part)
        raise
    progress(100, tr("Transferência concluída."))
    return dest


def fetch_sha256(release: ReleaseInfo, timeout: float = DEFAULT_TIMEOUT) -> str:
    """SHA-256 publicado para o ficheiro escolhido (ficheiro ``.sha256`` ou texto da versão)."""
    if release.asset is None:
        return ""
    if release.sha256_asset is not None:
        try:
            with _open_download(release.sha256_asset.url, timeout) as response:
                raw = response.read(MAX_SHA_FILE_BYTES)
        except UpdateError:
            raise
        except (OSError, ValueError) as exc:
            raise _network_error(exc) from exc
        digest = parse_sha256_text(raw.decode("utf-8", errors="replace"), release.asset.name)
        if not digest:
            raise UpdateError(tr("O ficheiro de verificação SHA-256 da versão não é válido."))
        return digest
    return release.sha256


def download_release(release: ReleaseInfo, progress: ProgressCallback = no_progress,
                     should_cancel: CancelCheck = never_cancel, dest_dir: Optional[str] = None) -> str:
    """Transfere o ficheiro da versão e devolve o caminho local (pronto a executar)."""
    asset = release.asset
    if asset is None:
        raise UpdateError(tr("Esta versão não inclui um ficheiro para Windows."))
    progress(0, tr("A preparar a transferência…"))
    expected_sha = fetch_sha256(release)
    if should_cancel():
        raise OperationCancelled()
    folder = dest_dir or download_dir()
    dest = os.path.join(folder, _safe_filename(asset.name))
    path = download_asset(asset.url, dest, progress, should_cancel,
                          expected_size=asset.size, expected_sha256=expected_sha)
    log.info("Atualização %s transferida para %s (SHA-256 %s)", release.version, path,
             "verificado" if expected_sha else "não publicado")
    return path


def launch_installer(path: str) -> None:
    """Abre o instalador transferido (o SafeOti deve fechar logo a seguir).

    Se o ficheiro não for um instalador (só ``SafeOti.exe``), mostra-o no
    Explorador em vez de o executar.
    """
    if not os.path.isfile(path):
        raise UpdateError(tr("O ficheiro transferido já não existe: {caminho}", caminho=path))
    if sys.platform != "win32":
        raise UpdateError(tr("A instalação automática só está disponível no Windows."))
    if fnmatch.fnmatch(os.path.basename(path).lower(), "safeoti-setup*.exe"):
        os.startfile(path)  # type: ignore[attr-defined]  # ShellExecute: respeita o manifesto/UAC
    else:
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])
