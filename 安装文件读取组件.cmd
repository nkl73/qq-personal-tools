@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -m pip install -r requirements.txt
) else (
    py -3 -m pip install -r requirements.txt
)
if errorlevel 1 (
    echo Install failed. Please check your Python and network connection.
) else (
    echo File readers installed. You can close this window.
)
popd
pause
