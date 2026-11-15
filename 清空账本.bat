@echo off
cd /d "%~dp0"
title Reset Ledger
if not exist "data" (echo No data folder. Nothing to do. & pause & exit /b)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%i
set BACKUP=data-backup-%STAMP%

echo Backing up to %BACKUP% ...
mkdir "%BACKUP%" 2>nul
if exist "data\stock.db" copy /y "data\stock.db" "%BACKUP%\" >nul
if exist "data\stock.db-wal" copy /y "data\stock.db-wal" "%BACKUP%\" >nul
if exist "data\stock.db-shm" copy /y "data\stock.db-shm" "%BACKUP%\" >nul
if exist "data\error.log" copy /y "data\error.log" "%BACKUP%\" >nul
if exist "data\snapshots" xcopy /e /i /y /q "data\snapshots" "%BACKUP%\snapshots" >nul

echo Deleting ledger files ...
if exist "data\stock.db" del /q "data\stock.db"
if exist "data\stock.db-wal" del /q "data\stock.db-wal"
if exist "data\stock.db-shm" del /q "data\stock.db-shm"
if exist "data\error.log" del /q "data\error.log"
if exist "data\console.log" del /q "data\console.log"
if exist "data\imported.db" del /q "data\imported.db"
if exist "data\_preview_tmp.db" del /q "data\_preview_tmp.db"
if exist "data\snapshots" rmdir /s /q "data\snapshots"

echo.
echo  Done. Backup kept in: %BACKUP%
echo  Kept: items.json, icon_cache, icon_map.json, stack_map.json
echo.
pause
