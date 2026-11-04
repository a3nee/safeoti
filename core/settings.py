"""Definições persistentes (JSON em %APPDATA%\\SafeOti) e configuração do log."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List

from .i18n import available_languages
from .utils import env_path

APP_NAME = "SafeOti"
SETTINGS_FILE = "settings.json"
LOG_FILE = "safeoti.log"

log = logging.getLogger("safeoti.settings")

DEFAULT_GAMING_APPS: List[str] = [
    "Discord.exe",
    "Spotify.exe",
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "opera.exe",
    "Teams.exe",
    "OneDrive.exe",
]

MIN_MONITOR_INTERVAL_MS = 500
MAX_MONITOR_INTERVAL_MS = 10000


def app_data_dir() -> Path:
    """Pasta de dados da aplicação (%APPDATA%\\SafeOti), criada se necessário."""
    base = env_path("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Último recurso: pasta temporária, para a app nunca falhar ao arrancar.
        path = Path(tempfile.gettempdir()) / APP_NAME
        path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(level: int = logging.INFO) -> Path:
    """Configura o log rotativo em %APPDATA%\\SafeOti\\logs. Devolve o caminho do ficheiro."""
    log_path = logs_dir() / LOG_FILE
    root = logging.getLogger("safeoti")
    root.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        root.addHandler(handler)
    return log_path


def normalize_process_name(name: str) -> str:
    """Normaliza um nome de processo: sem espaços extra e com extensão .exe."""
    clean = name.strip().strip('"')
    if not clean:
        return ""
    # Aceita caminhos completos e fica só com o nome do ficheiro.
    clean = clean.replace("/", "\\").split("\\")[-1]
    if not clean.lower().endswith(".exe"):
        clean += ".exe"
    return clean


THEMES = ("sistema", "claro", "escuro")


@dataclass
class Settings:
    gaming_close_apps: List[str] = field(default_factory=lambda: list(DEFAULT_GAMING_APPS))
    monitor_interval_ms: int = 1000
    start_with_windows: bool = False
    theme: str = "sistema"  # "sistema", "claro" ou "escuro"
    language: str = "pt"  # código de core.i18n.LANGUAGES
    check_updates: bool = True  # procurar novas versões ao abrir
    skipped_version: str = ""  # versão que o utilizador mandou ignorar

    def validate(self) -> "Settings":
        """Corrige valores inválidos (por exemplo, editados à mão no JSON)."""
        apps: List[str] = []
        seen = set()
        for item in self.gaming_close_apps if isinstance(self.gaming_close_apps, list) else []:
            if not isinstance(item, str):
                continue
            name = normalize_process_name(item)
            if name and name.lower() not in seen:
                seen.add(name.lower())
                apps.append(name)
        self.gaming_close_apps = apps
        try:
            interval = int(self.monitor_interval_ms)
        except (TypeError, ValueError):
            interval = 1000
        self.monitor_interval_ms = max(MIN_MONITOR_INTERVAL_MS, min(MAX_MONITOR_INTERVAL_MS, interval))
        self.start_with_windows = bool(self.start_with_windows)
        if self.theme not in THEMES:
            self.theme = "sistema"
        if self.language not in available_languages():
            self.language = "pt"
        self.check_updates = bool(self.check_updates)
        self.skipped_version = str(self.skipped_version or "").strip()[:40]
        return self


def settings_path() -> Path:
    return app_data_dir() / SETTINGS_FILE


def load_settings() -> Settings:
    """Lê as definições; se o ficheiro não existir ou estiver corrompido, usa as predefinições."""
    path = settings_path()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("formato inválido")
        known = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
        return Settings(**known).validate()
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Definições inválidas em %s (%s); a usar predefinições.", path, exc)
        try:
            backup = path.with_suffix(".json.corrompido")
            os.replace(path, backup)
            log.warning("Ficheiro inválido guardado como %s", backup)
        except OSError:
            pass
        return Settings()


def save_settings(settings: Settings) -> Path:
    """Guarda as definições de forma atómica (ficheiro temporário + substituição)."""
    settings.validate()
    path = settings_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    log.info(
        "Definições guardadas: %d apps no Modo Gaming, intervalo %d ms, iniciar com o Windows=%s",
        len(settings.gaming_close_apps),
        settings.monitor_interval_ms,
        settings.start_with_windows,
    )
    return path


def read_json_state(name: str) -> dict:
    """Lê um ficheiro de estado JSON auxiliar na pasta de dados (devolve {} se não existir)."""
    path = app_data_dir() / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Estado inválido em %s: %s", path, exc)
        return {}


def write_json_state(name: str, data: dict) -> None:
    path = app_data_dir() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def delete_json_state(name: str) -> None:
    try:
        (app_data_dir() / name).unlink()
    except FileNotFoundError:
        pass
