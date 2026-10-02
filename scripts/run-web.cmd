@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-web.ps1" %*
exit /b %ERRORLEVEL%
