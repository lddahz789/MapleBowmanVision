@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

:find_python
set "PYTHON="
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined PYTHON call :try_python "%%P"
)
for %%V in (314 313 312 311 310 39 38 37) do (
  if not defined PYTHON call :try_python "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
)
for %%V in (314 313 312 311 310 39 38 37) do (
  if not defined PYTHON call :try_python "%ProgramFiles%\Python%%V\python.exe"
)
if defined PYTHON goto :run_setup

echo 未找到可用的 Python，正在下载官方 Python 3.12.10……
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; try { $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $installer; $process = Start-Process -FilePath $installer -ArgumentList @('/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_pip=1', 'Include_test=0', 'SimpleInstall=1') -Wait -PassThru; if ($process.ExitCode -ne 0) { exit $process.ExitCode } } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
  echo Python 自动安装失败。请确认网络可用后重试 Setup.bat。
  echo 按任意键关闭窗口……
  pause >nul
  exit /b 1
)
goto :find_python

:run_setup
"%PYTHON%" setup_env.py
set "ERR=%ERRORLEVEL%"
echo.
echo 按任意键关闭窗口……
pause >nul
exit /b %ERR%

:try_python
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYTHON=%~1"
exit /b 0
