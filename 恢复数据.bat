@echo off
cd /d "%~dp0"
if not "%~1"=="" (echo Restoring from: %~1 & echo. & python app.py --restore-db "%~1" & echo. & echo Done. Start the app again. & pause & exit /b)
echo Drag a .db file onto this bat.
echo.
echo It will back up your current stock.db first, then replace it.
echo.
pause
