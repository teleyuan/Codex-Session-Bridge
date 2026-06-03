@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating isolated Python environment...
  python -m venv .venv
  if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo Installing PyInstaller into .venv...
  ".venv\Scripts\python.exe" -m pip install pyinstaller
  if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "codex_session_bridge" ^
  gui_entry.py

if errorlevel 1 goto :fail

echo.
echo Build succeeded: dist\codex_session_bridge.exe
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1

