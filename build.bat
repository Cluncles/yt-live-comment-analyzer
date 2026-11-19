@echo off
setlocal
cd /d "%~dp0"
set "PY="
python3 -V >nul 2>nul && set "PY=python3"
if not defined PY python -V >nul 2>nul && set "PY=python"
if not defined PY goto nopython
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 goto nopip
if exist requirements.txt "%PY%" -m pip install -r requirements.txt
echo Build complete. Run run.bat to start the server.
pause
exit /b 0
:nopython
echo Python 3 not found. Install it from https://www.python.org/downloads/ and enable "Add Python to PATH".
pause
exit /b 1
:nopip
echo pip not found. Reinstall Python with pip enabled.
pause
exit /b 1
