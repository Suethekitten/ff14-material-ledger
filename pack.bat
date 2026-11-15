@echo off
cd /d "%~dp0"
title ff14-stock packer

python --version >nul 2>nul
if %errorlevel%==0 goto RUN_PY

py -3 --version >nul 2>nul
if %errorlevel%==0 goto RUN_PYLAUNCHER

echo.
echo  [ERROR] Python not found.
echo  Install Python 3.9 or newer (tick "Add python.exe to PATH").
echo.
pause
exit /b 1

:RUN_PY
python pack.py
goto DONE

:RUN_PYLAUNCHER
py -3 pack.py

:DONE
echo.
pause
