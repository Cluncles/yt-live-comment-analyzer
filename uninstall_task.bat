@echo off
setlocal
set "NAME=YT Live Comment Analyzer"
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "%NAME%" /f
if errorlevel 1 goto fail
echo Logon startup removed: %NAME%
pause
exit /b 0
:fail
echo Startup entry not found or removal failed.
pause
exit /b 1
