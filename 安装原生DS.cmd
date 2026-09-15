@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="--check" goto check
python "%~dp0native_install.py" --activate
if errorlevel 1 goto failed
echo Installation completed.
echo Fully quit Codex, including the tray process, then use the new desktop shortcut.
pause
exit /b 0

:failed
echo Installation did not complete. Keep the error output above for diagnosis.
pause
exit /b 1

:check
python "%~dp0native_install.py" --help
exit /b %errorlevel%
