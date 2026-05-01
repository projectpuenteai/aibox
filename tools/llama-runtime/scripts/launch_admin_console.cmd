@echo off
REM Manual fallback launcher for Consola Puente Admin.
REM Use when the desktop shortcut is broken. The script self-elevates
REM via UAC and shows a friendly window if you click No.
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0aibox_control_ui.ps1"
endlocal
