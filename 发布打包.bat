@echo off
cd /d "%~dp0"
title Release Pack

python pack_release.py
if errorlevel 1 goto FAIL
echo.
pause
exit /b 0

:FAIL
echo.
echo  [ERROR] Release pack failed. See messages above.
echo  Make sure you ran build_exe.bat first.
echo.
pause
exit /b 1
