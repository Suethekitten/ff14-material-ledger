@echo off
REM Build a Windows EXE with PyInstaller (onedir mode).
REM Output: dist\ff14-stock\ff14-stock.exe
REM The window is opened by system Edge in --app mode, so no pywebview needed.
cd /d "%~dp0"
title Build EXE

echo [1/4] syncing version from version.json...
python tools\sync_version.py
if errorlevel 1 goto FAIL

echo [2/4] installing build dependency...
python -m pip install --upgrade pyinstaller >nul
if errorlevel 1 goto FAIL

echo [3/4] building (this takes 1-3 minutes)...
python -m PyInstaller --noconfirm --clean ^
  --name ff14-stock ^
  --onedir ^
  --console ^
  --icon "icon.ico" ^
  --add-data "static;static" ^
  --add-data "data\items.json;data" ^
  --add-data "data\recipe.json;data" ^
  --add-data "data\npc_shop.json;data" ^
  app.py
if errorlevel 1 goto FAIL

echo [4/4] done.
echo.
echo  Output: dist\ff14-stock\ff14-stock.exe
echo  Send the WHOLE folder dist\ff14-stock, not just the exe.
echo.
pause
exit /b 0

:FAIL
echo.
echo  [ERROR] Build failed. See messages above.
echo  If pyinstaller is missing:  python -m pip install pyinstaller
echo.
pause
exit /b 1
