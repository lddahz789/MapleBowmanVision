@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 找不到 python 命令。请先安装 Python，或把 python.exe 加入 PATH。
  echo 按任意键关闭窗口……
  pause >nul
  exit /b 1
)
python setup_env.py
set "ERR=%ERRORLEVEL%"
echo.
echo 按任意键关闭窗口……
pause >nul
exit /b %ERR%
