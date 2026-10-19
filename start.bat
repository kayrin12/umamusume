@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist .runtime\python\python.exe (
  .runtime\python\python.exe -E -s -m umacoach.webapp
  if errorlevel 1 goto fail
  exit /b 0
)
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m umacoach.webapp
if errorlevel 1 goto fail
exit /b 0
:fail
echo 启动失败，请保留此窗口的报错文字以便排查。
echo 便携版请先完整解压，再运行 start.bat，不要只复制启动脚本。
pause
exit /b 1
