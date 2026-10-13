@echo off
rem =========================================================================
rem Telegram Remote Control - Service Launcher
rem Launches AG, Nova, and Remote Control Agent / Controller via PowerShell.
rem =========================================================================

setlocal
set SCRIPT_DIR=%~dp0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\start_services.ps1" %*
endlocal
