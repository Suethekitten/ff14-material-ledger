@echo off
cd /d "%~dp0"
title FF14 Stock - Diagnostic

echo ===== run at %date% %time% ===== > run.log
echo CWD: %cd% >> run.log
where python >> run.log 2>&1
python --version >> run.log 2>&1
echo ---- app output ---- >> run.log

python app.py >> run.log 2>&1
set RC=%errorlevel%
echo ---- exit code: %RC% ---- >> run.log

echo.
echo  Diagnostic finished. Exit code: %RC%
echo  Log saved to: run.log
echo.
echo  Send run.log to the developer if the window flashes.
pause
