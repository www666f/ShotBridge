@echo off
chcp 65001 >nul
title ShotBridge Self-Check
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%installer\verify.ps1"
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" (
  echo [FAILED] exit code %CODE%
) else (
  echo [OK] done, exit code 0
)
echo.
pause
exit /b %CODE%
