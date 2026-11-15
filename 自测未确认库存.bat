@echo off
cd /d "%~dp0"
python tools\test_pending.py
echo.
pause
