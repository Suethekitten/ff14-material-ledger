@echo off
cd /d "%~dp0"
title Export Stock CSV

if "%~1"=="" (
  if exist "data\stock.db" (
    python tools\db_to_csv.py "data\stock.db"
  ) else (
    echo Drag a .db file onto this bat, or run:
    echo    python tools\db_to_csv.py "D:\path\stock.db"
  )
) else (
  python tools\db_to_csv.py "%~1"
)

echo.
pause
