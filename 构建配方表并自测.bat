@echo off
cd /d "%~dp0"
echo === 1) 构建配方表（需要联网，约 2.4MB）===
python app.py --build-recipes
echo.
echo === 2) 跑展开算法自测 ===
python tools\test_recipe.py
echo.
pause
