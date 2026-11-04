"""Idioma da interface: catálogo do SafeOti (core.i18n) + textos internos do Qt."""

from __future__ import annotations

import logging

from core import i18n

log = logging.getLogger("safeoti.ui.localization")

_qt_translator = None


def apply(app, language: str) -> str:
    """Ativa ``language`` na app inteira. Devolve o idioma efetivamente ativo."""
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    global _qt_translator
    language = i18n.set_language(language)

    if _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator = None

    if language == "pt":
        locale = QLocale(QLocale.Portuguese, QLocale.Portugal)
        path = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
        translator = QTranslator(app)
        if translator.load(locale, "qtbase", "_", path) or translator.load("qtbase_pt", path):
            app.installTranslator(translator)
            _qt_translator = translator
    else:
        # O Qt já vem em inglês: não é preciso tradutor.
        locale = QLocale(QLocale.English, QLocale.UnitedKingdom)
    QLocale.setDefault(locale)
    log.info("Idioma da interface: %s", language)
    return language
