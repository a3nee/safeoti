@echo off
REM ==========================================================================
REM  SafeOti - compila um unico SafeOti.exe com PyInstaller.
REM  O executavel inclui um manifesto que pede privilegios de administrador.
REM  Uso: build.bat            (cria .venv, instala dependencias, testa e compila)
REM       build.bat --sem-testes
REM ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON=python"
where %PYTHON% >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERRO] Python nao encontrado. Instale o Python 3.11 ou superior a partir de https://www.python.org
        echo        e marque a opcao "Add python.exe to PATH".
        exit /b 1
    )
    set "PYTHON=py -3"
)

%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERRO] E necessario o Python 3.11 ou superior.
    %PYTHON% --version
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] A criar o ambiente virtual .venv ...
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo [ERRO] Nao foi possivel criar o ambiente virtual.
        exit /b 1
    )
) else (
    echo [1/4] Ambiente virtual .venv ja existe.
)

set "VPY=.venv\Scripts\python.exe"

echo [2/4] A instalar dependencias ...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERRO] Falha ao instalar as dependencias.
    exit /b 1
)

if /I "%~1"=="--sem-testes" (
    echo [3/4] Testes ignorados.
) else (
    echo [3/4] A executar os testes ...
    "%VPY%" -m unittest discover -s tests
    if errorlevel 1 (
        echo [ERRO] Os testes falharam. Corrija os erros ou use: build.bat --sem-testes
        exit /b 1
    )
)

echo [4/4] A compilar SafeOti.exe ...
if exist "build" rmdir /s /q "build"
if exist "dist\SafeOti.exe" del /q "dist\SafeOti.exe"
mkdir build
"%VPY%" -m installer.make_icon build\SafeOti.ico
if errorlevel 1 (
    echo [ERRO] Nao foi possivel gerar o icone.
    exit /b 1
)

"%VPY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name SafeOti ^
    --uac-admin ^
    --manifest SafeOti.manifest ^
    --icon build\SafeOti.ico ^
    --add-data "ui\styles.qss;ui" ^
    --add-data "ui\check.svg;ui" ^
    --add-data "ui\arrow-up.svg;ui" ^
    --add-data "ui\arrow-down.svg;ui" ^
    --add-data "i18n\en.json;i18n" ^
    --exclude-module tkinter ^
    --exclude-module PyQt5 ^
    --exclude-module PyQt6 ^
    --exclude-module PySide2 ^
    --exclude-module PySide6.QtWebEngineCore ^
    --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.Qt3DCore ^
    --exclude-module PySide6.QtQuick ^
    --exclude-module PySide6.QtQml ^
    --exclude-module PySide6.QtMultimedia ^
    --exclude-module PySide6.QtPdf ^
    main.py
if errorlevel 1 (
    echo [ERRO] A compilacao falhou. Veja as mensagens acima.
    exit /b 1
)

echo.
echo Concluido: dist\SafeOti.exe
echo Ao abrir o executavel, o Windows pede confirmacao de administrador (UAC).
endlocal
exit /b 0
