@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python task_app.pyw
) else (
    py -3 task_app.pyw
)
popd
