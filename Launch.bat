@echo off
rem Double-click to run PhantomFrame. Sets it up first if needed.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    set "PHANTOMFRAME_DIR=%~dp0."
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
    exit /b
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0gta6_framegen.py"
