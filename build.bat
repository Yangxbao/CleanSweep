@echo off
rem 打包 CleanSweep 为单文件 exe：dist\CleanSweep.exe
rem --onefile 单文件  --windowed 无控制台  --uac-admin 启动时自动请求管理员权限
setlocal
cd /d "%~dp0"
echo ==============================================
echo  CleanSweep 打包：生成 dist\CleanSweep.exe
echo ==============================================
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
echo 使用解释器: %PY%
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :err
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name CleanSweep main.py
if errorlevel 1 goto :err
echo.
echo 打包完成：%~dp0dist\CleanSweep.exe
pause
exit /b 0
:err
echo 打包失败，请检查上方错误信息。
pause
exit /b 1
