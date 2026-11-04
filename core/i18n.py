"""Tradução da interface (sem PySide6).

O texto de origem é o português (pt-PT). Para outro idioma, ``tr`` procura a
frase num catálogo JSON em ``i18n/<idioma>.json`` (``{"texto pt": "tradução"}``);
se não existir tradução, devolve o texto original.

Uso::

    from core.i18n import tr
    label.setText(tr("Espaço livre: {size}", size=format_bytes(n)))

Os marcadores ``{nome}`` são preenchidos com ``str.format`` *depois* da
tradução, por isso a chave do catálogo é sempre o texto com os marcadores.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

log = logging.getLogger("safeoti.i18n")

SOURCE_LANGUAGE = "pt"
# (código, nome mostrado no seletor — sempre no próprio idioma)
LANGUAGES: List[Tuple[str, str]] = [("pt", "Português"), ("en", "English")]

_language = SOURCE_LANGUAGE
_catalog: Dict[str, str] = {}


def _i18n_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "i18n"
    return Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "i18n"


def available_languages() -> List[str]:
    return [code for code, _name in LANGUAGES]


def load_catalog(language: str) -> Dict[str, str]:
    """Lê ``i18n/<language>.json``; devolve {} se não existir ou for inválido."""
    if language == SOURCE_LANGUAGE:
        return {}
    path = _i18n_dir() / f"{language}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("Catálogo de tradução não encontrado: %s", path)
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Catálogo de tradução inválido %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def set_language(language: str) -> str:
    """Ativa um idioma. Idiomas desconhecidos passam a português."""
    global _language, _catalog
    if language not in available_languages():
        language = SOURCE_LANGUAGE
    _language = language
    _catalog = load_catalog(language)
    return _language


def current_language() -> str:
    return _language


def tr(text: str, **values: object) -> str:
    """Traduz ``text`` para o idioma ativo e preenche os marcadores ``{nome}``."""
    translated = _catalog.get(text, text) if _catalog else text
    if values:
        try:
            return translated.format(**values)
        except (KeyError, IndexError, ValueError):
            # Tradução com marcadores errados: usa o original para não rebentar.
            try:
                return text.format(**values)
            except (KeyError, IndexError, ValueError):
                return text
    return translated


def decimal_separator() -> str:
    return "." if _language == "en" else ","


def format_decimal(value: float, decimals: int = 1) -> str:
    """Número com casas decimais e o separador do idioma ativo."""
    text = f"{value:.{decimals}f}"
    return text.replace(".", decimal_separator())
