@echo off
REM Diagnostic wrapper: captures host-level powershell.exe stdout/stderr
REM that the in-script bootstrap log cannot see. Use when the UI fails to
REM appear and ui-bootstrap.log shows nothing recent.
setlocal
set "LOGDIR=%~dp0..\..\..\backend-data\appdata\host-admin"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "TRACE=%LOGDIR%\launcher-trace.log"
> "%TRACE%" echo [%date% %time%] launcher invoked, args=%*
>>"%TRACE%" echo [%date% %time%] cwd=%cd%  pwd_script=%~dp0
powershell.exe -NoLogo -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0aibox_control_ui.ps1" -NoElevate 1>>"%TRACE%" 2>&1
>>"%TRACE%" echo [%date% %time%] powershell exited with %ERRORLEVEL%
endlocal
