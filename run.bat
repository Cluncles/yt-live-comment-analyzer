@echo off
setlocal
cd /d "%~dp0"
set "PY="
python3 -V >nul 2>nul && set "PY=python3"
if not defined PY python -V >nul 2>nul && set "PY=python"
if not defined PY goto nopython
"%PY%" server.py --host 0.0.0.0 --port 8765
exit /b 0
:nopython
echo Python 3 not found. Run build.bat first or install Python 3.
pause
exit /b 1
