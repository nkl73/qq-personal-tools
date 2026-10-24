@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -X utf8 install_background.py
) else (
    py -3 -X utf8 install_background.py
)
pause
popd
