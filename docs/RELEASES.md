# Publicar uma versão do SafeOti

O SafeOti procura atualizações em
`https://api.github.com/repos/a3nee/safeoti/releases/latest` (ver `core/updater.py`).
Para que o atualizador encontre uma nova versão:

## 1. Atualizar o número da versão

Altere `__version__` em `core/__init__.py` (por exemplo `"1.1.0"`). A etiqueta (tag)
da versão no GitHub tem de corresponder: `v1.1.0`.

Formatos aceites: `1.2.3`, `1.2`, `v1.2.3`. Etiquetas com sufixo (`v1.2.3-beta`)
são consideradas pré-versões e **não** são oferecidas aos utilizadores.

## 2. Compilar

```bat
build.bat
build_installer.bat
```

Resultado: `dist\SafeOti.exe` e `dist\SafeOti-Setup.exe`.

## 3. (Opcional, recomendado) Gerar o SHA-256

```powershell
$h = (Get-FileHash dist\SafeOti-Setup.exe -Algorithm SHA256).Hash.ToLower()
"$h  SafeOti-Setup.exe" | Set-Content -Encoding ascii dist\SafeOti-Setup.exe.sha256
```

O atualizador verifica o hash se encontrar um ficheiro `*.sha256` anexado à versão
ou uma linha `SHA256: <hex>` nas notas da versão. Se não corresponder, o ficheiro
transferido é apagado.

## 4. Publicar no GitHub

```powershell
git tag v1.1.0
git push origin v1.1.0
gh release create v1.1.0 dist\SafeOti-Setup.exe dist\SafeOti-Setup.exe.sha256 `
    --title "SafeOti 1.1.0" --notes-file NOTAS.md
```

- Anexe **`SafeOti-Setup.exe`** (preferido). Se não existir, é usado um ficheiro
  `SafeOti*.exe`.
- As notas (Markdown) aparecem na janela "Atualização disponível".
- Não marque a versão como *draft* nem como *pre-release*: essas são ignoradas.

## Comportamento na aplicação

- Ao arrancar (se "check_updates" estiver ativo) a verificação é silenciosa: só
  aparece uma janela se houver uma versão mais recente que não tenha sido ignorada.
- O botão "Procurar atualizações" mostra sempre o resultado ("Já tem a versão mais
  recente", "Ainda não há versões publicadas" ou o erro).
- As transferências só são aceites por HTTPS a partir de `github.com` /
  `objects.githubusercontent.com` (e respetivos redirecionamentos).
