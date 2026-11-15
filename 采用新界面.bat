@echo off
cd /d "%~dp0"
if not exist "..\ff14-stock-ui-lab\static\index.html" (echo [ERROR] Missing ..\ff14-stock-ui-lab\static\index.html & pause & exit /b 1)
if exist "static\index.html" copy /y "static\index.html" "static\index.legacy.html" >nul
copy /y "..\ff14-stock-ui-lab\static\index.html" "static\index.html" >nul
echo.
echo  Done. Restart the app, then press Ctrl+F5 in the browser.
echo.
pause
