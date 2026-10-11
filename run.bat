@echo off
rem 从源码启动 CleanSweep（首次弹出 UAC 提权询问，选“是”获得完整清理能力）
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 python。请先安装 Python 3.8+，安装时勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
python main.py %*
if errorlevel 1 pause
