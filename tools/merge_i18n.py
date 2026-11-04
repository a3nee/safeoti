"""Junta i18n/parts/*.json em i18n/en.json e verifica as chaves usadas no código.

Uso: python tools/merge_i18n.py
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "main.py", *sorted((ROOT / "ui").rglob("*.py")), *sorted((ROOT / "core").glob("*.py"))]


def used_keys() -> dict:
    keys = {}
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.setdefault(node.args[0].value, f"{path.relative_to(ROOT)}:{node.lineno}")
    return keys


def main() -> int:
    # A consola do Windows usa cp1252/cp850 e rebenta com "→" e acentos.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    merged: dict = {}
    target = ROOT / "i18n" / "en.json"
    if target.exists():
        merged.update(json.loads(target.read_text(encoding="utf-8")))
    for part in sorted((ROOT / "i18n" / "parts").glob("*.json")):
        for key, value in json.loads(part.read_text(encoding="utf-8")).items():
            if key in merged and merged[key] != value:
                print(f"aviso: tradução diferente para {key!r} em {part.name}: {value!r} (mantém {merged[key]!r})")
                continue
            merged[key] = value
    used = used_keys()
    missing = {k: where for k, where in used.items() if k not in merged}
    for key, where in missing.items():
        print(f"em falta: {where}: {key!r}")
    target.write_text(json.dumps(dict(sorted(merged.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(merged)} traduções, {len(used)} chaves usadas, {len(missing)} em falta")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
