@echo off
cd /d "%~dp0"
title Restore DB

if "%~1"=="" (
  echo Usage: drag a .db file onto this bat, or:
  echo    python app.py --restore-db "D:\path\stock.db"
  echo.
  pause
  exit /b 1
)

echo Restoring from: %~1
echo.
python app.py --restore-db "%~1"
echo.
pause
