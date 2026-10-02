@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-venv.ps1" %*
exit /b %ERRORLEVEL%
