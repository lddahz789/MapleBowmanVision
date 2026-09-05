@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "PYTHONW=%ROOT%.venv\Scripts\pythonw.exe"
cd /d "%ROOT%"

if /I "%~1"=="--elevated" (
  set "PROFILE=%~2"
  goto :profile
)

set "PROFILE=%~1"
if not defined PROFILE set "PROFILE=newmaple"
goto :elevate

:profile
if not defined PROFILE set "PROFILE=newmaple"
if /I "%PROFILE%"=="classic" goto :validate
if /I "%PROFILE%"=="newmaple" goto :validate
echo 不支持的运行档案：%PROFILE%。请使用 newmaple 或 classic。
pause >nul
exit /b 2

:validate
rem 不能只检查文件是否存在：虚拟环境可能仍在，但它指向的 Python 已被删除。
if not exist "%PYTHON%" goto :repair
"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :repair
if not exist "%PYTHONW%" goto :repair

:run
start "" /b "%PYTHONW%" "%ROOT%maple_bowman.py" --profile "%PROFILE%" --enable-input
exit /b 0

:elevate
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList @('--elevated','%PROFILE%') -WorkingDirectory '%ROOT%' -Verb RunAs"
if errorlevel 1 (
  echo 无法请求管理员权限，请在 UAC 提示中选择“是”。
  pause >nul
  exit /b 1
)
exit /b 0

:repair
echo 当前运行环境不可用，正在自动修复。若出现 UAC 或安装提示，请按提示操作……
call "%ROOT%Setup.bat"
if errorlevel 1 exit /b 1
goto :validate
