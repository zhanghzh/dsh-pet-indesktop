@echo off
rem ============================================
rem  dsh-pet standalone pet - one-click launcher
rem  (double-click to start; no console window)
rem  Data dir redirected to .\data so config,
rem  logs and caches stay inside this folder.
rem ============================================
cd /d "%~dp0"
set "APPDATA=%~dp0data"
if not exist "%APPDATA%" mkdir "%APPDATA%"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m pet
) else (
    start "" pythonw -m pet
)
