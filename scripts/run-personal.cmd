@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-personal.ps1" %*
exit /b %ERRORLEVEL%
