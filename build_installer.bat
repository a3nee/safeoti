@echo off
REM ==========================================================================
REM  SafeOti - compila o instalador personalizado dist\SafeOti-Setup.exe
REM  1) compila dist\SafeOti.exe (build.bat)   2) embute-o no instalador.
REM  Uso: build_installer.bat [--sem-testes]
REM ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

call "%~dp0build.bat" %1
if errorlevel 1 exit /b 1

set "VPY=.venv\Scripts\python.exe"
echo.
echo [5/5] A compilar o instalador SafeOti-Setup.exe ...
if exist "dist\SafeOti-Setup.exe" del /q "dist\SafeOti-Setup.exe"

"%VPY%" -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name SafeOti-Setup ^
    --icon "%~dp0build\SafeOti.ico" ^
    --workpath build\setup ^
    --specpath build ^
    --add-data "%~dp0dist\SafeOti.exe;payload" ^
    --exclude-module tkinter ^
    --exclude-module PyQt5 ^
    --exclude-module PyQt6 ^
    --exclude-module PySide2 ^
    --exclude-module pyqtgraph ^
    --exclude-module numpy ^
    --exclude-module psutil ^
    --exclude-module PySide6.QtWebEngineCore ^
    --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.Qt3DCore ^
    --exclude-module PySide6.QtQuick ^
    --exclude-module PySide6.QtQml ^
    --exclude-module PySide6.QtMultimedia ^
    --exclude-module PySide6.QtPdf ^
    --exclude-module PySide6.QtOpenGL ^
    --exclude-module PySide6.QtNetwork ^
    installer\setup_main.py
if errorlevel 1 (
    echo [ERRO] A compilacao do instalador falhou.
    exit /b 1
)

echo.
echo Concluido: dist\SafeOti-Setup.exe
endlocal
exit /b 0
