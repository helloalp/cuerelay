@echo off
setlocal
cd /d "%~dp0"

rem Use the windowless Python launcher when it is available.
where pythonw.exe >nul 2>nul
if not errorlevel 1 goto launch_pythonw

where pyw.exe >nul 2>nul
if not errorlevel 1 goto launch_pyw

where python.exe >nul 2>nul
if not errorlevel 1 goto launch_python

echo Python 3 was not found. Install Python 3.10 or newer and enable PATH.
pause
exit /b 1

:launch_pythonw
start "" pythonw.exe "%~dp0cuerelay.py"
exit /b 0

:launch_pyw
start "" pyw.exe -3 "%~dp0cuerelay.py"
exit /b 0

:launch_python
start "" python.exe "%~dp0cuerelay.py"
exit /b 0
