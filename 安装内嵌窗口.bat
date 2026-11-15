@echo off
cd /d "%~dp0"
title Install pywebview

echo Installing pywebview (needed for the embedded window) ...
python -m pip install --upgrade pywebview
if errorlevel 1 goto FAIL

echo.
echo  Done. Next time you run 启动.bat, it will open in its own window.
echo  To force the browser instead:  python app.py --browser
echo.
pause
exit /b 0

:FAIL
echo.
echo  [ERROR] pip install failed.
echo  Check your network, or run manually:
echo     python -m pip install pywebview
echo.
pause
exit /b 1
