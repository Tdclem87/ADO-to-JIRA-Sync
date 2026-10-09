@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update.ps1"
set "RESULT=%ERRORLEVEL%"

echo.
if not "%RESULT%"=="0" echo Update did not complete.
pause
exit /b %RESULT%