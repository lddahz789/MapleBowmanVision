@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python batch_messages.py need_setup
  python batch_messages.py close_window
  pause >nul
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" maple_bowman.py --capture-player-title
exit /b 0
