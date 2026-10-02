@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-doctor.ps1" %*
exit /b %ERRORLEVEL%
