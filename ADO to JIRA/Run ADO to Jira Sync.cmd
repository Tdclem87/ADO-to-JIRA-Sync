@echo off
setlocal
pushd "%~dp0"

where powershell.exe >nul 2>&1
if %errorlevel% equ 0 (
	powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ADO to Jira Sync.ps1"
	if errorlevel 1 (
		pause
		exit /b 1
	)
	exit /b 0
)

echo PowerShell is unavailable; automatic update check skipped.
set "SCRIPT=clean_ado_to_jira_sync.py"
set "VENV=%~dp0..\.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
if not exist "%PYTHON%" (
	where py.exe >nul 2>&1
	if not errorlevel 1 (
		py -3 -m venv "%VENV%"
	) else (
		where python.exe >nul 2>&1
		if errorlevel 1 goto missing_python
		python -m venv "%VENV%"
	)
	if errorlevel 1 goto setup_failed
)

"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 echo WARNING: pip could not be updated; continuing with the installed version.

"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto setup_failed

:restart
echo.
echo ===== Azure DevOps to Jira Sync =====
echo Starting a new run with %SCRIPT%...
echo Using Python: %PYTHON%
echo.
"%PYTHON%" "%SCRIPT%"
echo.
echo Run completed.
echo The session will restart automatically.
echo Restarting from the first prompt...
goto restart

:missing_python
echo Python 3 is required to run this app. Install Python and launch again.
pause
exit /b 1

:setup_failed
echo The Python environment or required packages could not be installed.
echo Check your network connection and Python installation, then launch again.
pause
exit /b 1
