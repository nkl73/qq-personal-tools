@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python app.pyw
) else (
    py -3 app.pyw
)
popd
