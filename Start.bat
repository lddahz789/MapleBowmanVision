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
if /I "%~1"=="--elevated" goto :run
powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs"
exit /b 0
:run
start "" ".venv\Scripts\pythonw.exe" maple_bowman.py --enable-input
exit /b 0
