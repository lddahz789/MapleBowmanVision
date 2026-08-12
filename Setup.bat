@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :early_error
)
".venv\Scripts\python.exe" batch_messages.py setup_check
".venv\Scripts\python.exe" -m pip --version
if errorlevel 1 goto :error
echo.
".venv\Scripts\python.exe" batch_messages.py setup_install_mirror
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --timeout 30 --retries 3 -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if not errorlevel 1 goto :success
".venv\Scripts\python.exe" batch_messages.py setup_retry_default
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --timeout 30 --retries 3 -r requirements.txt
if errorlevel 1 goto :error
:success
echo.
".venv\Scripts\python.exe" batch_messages.py setup_success
".venv\Scripts\python.exe" batch_messages.py close_window
pause >nul
exit /b 0
:error
echo.
".venv\Scripts\python.exe" batch_messages.py setup_failed
".venv\Scripts\python.exe" batch_messages.py close_window
pause >nul
exit /b 1
:early_error
python batch_messages.py setup_failed
python batch_messages.py close_window
pause >nul
exit /b 1
