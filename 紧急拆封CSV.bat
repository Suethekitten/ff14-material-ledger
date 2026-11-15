@echo off
cd /d "%~dp0"
if not "%~1"=="" (echo Using: %~1 & python tools\emergency_export.py "%~1") else (if exist "data\stock.db" (echo Using: data\stock.db & python tools\emergency_export.py "data\stock.db") else (echo No data\stock.db here. & echo Drag your stock.db onto this bat.))
echo.
pause
