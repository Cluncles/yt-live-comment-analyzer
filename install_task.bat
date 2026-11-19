@echo off
setlocal
set "NAME=YT Live Comment Analyzer"
set "RUN=%~dp0run.bat"
set "VBS=%~dp0launch.vbs"
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "%NAME%" /t REG_SZ /d "wscript.exe ""%VBS%"" ""%RUN%""" /f
if errorlevel 1 goto fail
echo Logon startup installed: %NAME%
echo The server starts hidden at each logon.
pause
exit /b 0
:fail
echo Could not write the startup entry.
pause
exit /b 1
